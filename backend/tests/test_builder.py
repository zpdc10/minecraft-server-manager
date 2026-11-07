"""Tests for the install, test-start and fix loop, the crash analyzer and the mod list."""

import dataclasses
import json
import os
import zipfile

import httpx
import pytest
import respx
from docker.errors import ImageNotFound, NotFound

from app.config import Settings
from app.models.server import CreateServerRequest
from app.services import mod_db as mod_db_module
from app.services import server_builder
from app.services.crash_analyzer import analyze
from app.services.docker_manager import DockerManager
from app.services.mod_db import ModDB
from app.services.mod_scan import ModJar, read_jar, scan_mods

# ---- jar helpers ------------------------------------------------------------------


def make_jar(path, *, fabric=None, forge=None, classes=(), extra=None):
    with zipfile.ZipFile(path, "w") as z:
        if fabric is not None:
            z.writestr("fabric.mod.json", json.dumps(fabric))
        if forge is not None:
            z.writestr("META-INF/mods.toml", forge)
        for cls in classes:
            z.writestr(cls.replace(".", "/") + ".class", b"\xca\xfe")
        for name, data in (extra or {}).items():
            z.writestr(name, data)


def forge_toml(mod_id, name=None, client_only=False):
    head = "clientSideOnly=true\n" if client_only else ""
    return f'{head}modLoader="javafml"\n[[mods]]\nmodId="{mod_id}"\ndisplayName="{name or mod_id}"\n'


# ---- mod_scan ---------------------------------------------------------------------


def test_read_fabric_jar(tmp_path):
    p = tmp_path / "sodium-fabric-0.5.jar"
    make_jar(
        p,
        fabric={"id": "sodium", "name": "Sodium", "environment": "client", "mixins": ["sodium.mixins.json"]},
        classes=["me.jellysquid.mods.sodium.Main"],
    )
    jar = read_jar(str(p))
    assert jar.mod_ids == ["sodium"]
    assert jar.name == "Sodium"
    assert jar.client_only
    assert "sodium.mixins.json" in jar.mixin_configs
    assert "me.jellysquid.mods" in jar.packages


def test_read_forge_jar_and_bad_toml(tmp_path):
    p = tmp_path / "a.jar"
    make_jar(p, forge=forge_toml("coolmod", "Cool Mod", client_only=True))
    jar = read_jar(str(p))
    assert jar.mod_ids == ["coolmod"] and jar.name == "Cool Mod" and jar.client_only
    p2 = tmp_path / "b.jar"
    make_jar(p2, forge='modLoader="javafml"\n[[mods]]\nmodId="broken"\nversion="${file.jarVersion\n')
    assert read_jar(str(p2)).mod_ids == ["broken"]


def test_scan_skips_non_jars_and_bad_zips(tmp_path):
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "bad.jar").write_text("not a zip")
    jars = scan_mods(str(tmp_path))
    assert [j.filename for j in jars] == ["bad.jar"]
    assert scan_mods(str(tmp_path / "missing")) == []


# ---- crash analyzer ---------------------------------------------------------------


def jar(filename, ids, packages=(), mixins=()):
    return ModJar(filename=filename, mod_ids=list(ids), packages=set(packages), mixin_configs=set(mixins))


def test_client_class_on_server_blames_mod_in_stack():
    jars = [jar("badmod-1.0.jar", ["badmod"], ["com.bad.mod"]), jar("other.jar", ["other"], ["org.other"])]
    log = (
        "[main/ERROR]: Exception loading mods\n"
        "java.lang.RuntimeException: Attempted to load class net/minecraft/client/gui/screens/Screen "
        "for invalid dist DEDICATED_SERVER\n"
        "\tat net.minecraftforge.fml.loading.RuntimeDistCleaner.processClassWithFlags(RuntimeDistCleaner.java:57)\n"
        "\tat TRANSFORMER/badmod@1.0/com.bad.mod.ClientSetup.init(ClientSetup.java:10)\n"
    )
    a = analyze(log, jars)
    assert [s.jar.filename for s in a.suspects] == ["badmod-1.0.jar"]
    assert a.suspects[0].reason == "client_only"


def test_client_class_blamed_by_plain_stack_frame():
    jars = [jar("x.jar", ["xmod"], ["com.x.mod"])]
    log = (
        "java.lang.NoClassDefFoundError: net/minecraft/client/Minecraft\n\tat com.x.mod.Thing.<clinit>(Thing.java:3)\n"
    )
    assert analyze(log, jars).suspects[0].jar.filename == "x.jar"


def test_forge_missing_dependency_blames_requester():
    jars = [jar("needs.jar", ["needsbad"])]
    log = "\tMod ID: 'badmod', Requested by: 'needsbad', Expected range: '[1,)', Actual version: '[MISSING]'\n"
    a = analyze(log, jars)
    assert a.suspects[0].jar.filename == "needs.jar"
    assert a.suspects[0].reason == "missing_dependency"


def test_fabric_missing_dependency():
    jars = [jar("addon.jar", ["addon"])]
    log = " - Mod 'Some Addon' (addon) 1.2 requires version 0.5 or later of mod 'Sodium' (sodium), which is missing!\n"
    a = analyze(log, jars)
    assert a.suspects[0].jar.filename == "addon.jar"
    assert "sodium" in a.suspects[0].detail


def test_mixin_failure_maps_config_to_jar():
    jars = [jar("mix.jar", ["mixmod"], mixins=["mixmod.mixins.json"])]
    log = (
        "org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: An unexpected critical error"
        " was encountered\nCaused by: org.spongepowered.asm.mixin.injection.throwables.InvalidInjectionException:"
        " Critical injection failure: Mixin [mixmod.mixins.json:client.ScreenMixin] failed injection check\n"
    )
    assert analyze(log, jars).suspects[0].jar.filename == "mix.jar"


def test_forge_failure_message():
    jars = [jar("cm.jar", ["coolmod"])]
    log = "\tFailure message: Cool Mod (coolmod) has failed to load correctly\n"
    assert analyze(log, jars).suspects[0].jar.filename == "cm.jar"


def test_core_frames_are_not_blamed():
    jars = [jar("x.jar", ["xmod"], ["com.x.mod"])]
    log = "---- Minecraft Crash Report ----\n\tat net.minecraft.server.Main.main(Main.java:1)\n"
    a = analyze(log, jars)
    assert a.suspects == []


def test_non_mod_problems():
    assert "memory" in analyze("java.lang.OutOfMemoryError: Java heap space", []).fatal
    assert "port" in analyze("**** FAILED TO BIND TO PORT!", []).fatal
    log = (
        "java.lang.UnsupportedClassVersionError: x has been compiled by a more recent version of the Java Runtime"
        " (class file version 65.0), this version of the Java Runtime only recognizes class file versions up to 61.0"
    )
    assert analyze(log, []).java_needed == 21


# ---- mod db -----------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(
        Settings(),
        data_dir=str(tmp_path / "app"),
        data_root=str(tmp_path / "servers"),
        host_data_root="/mnt/user/appdata/mc-servers",
        mod_db_url="",
        build_max_attempts=5,
    )


def test_bundled_list_matches_ids_and_file_names(settings):
    match = ModDB(settings).matcher("forge")
    assert match(jar("whatever.jar", ["oculus"])).reason == "client_only"
    assert match(jar("Zoomify-2.1.jar", ["zoomify_x"])) is not None
    # Substrings inside longer words don't count.
    assert match(jar("blurry-blocks-1.0.jar", ["blurryblocks"])) is None
    assert match(jar("create-0.5.jar", ["create"])) is None


def test_learn_and_forget(settings):
    db = ModDB(settings)
    j = jar("fancyhud-1.0.jar", ["fancyhud"])
    db.learn(j, "crash", "boom", "Pack", "forge")
    assert db.learned() == []  # pack-specific crashes aren't learned
    db.learn(j, "client_only", "client class", "Pack", "forge")
    db.learn(j, "client_only", "client class", "Pack 2", "forge")
    assert db.learned()[0]["count"] == 2
    assert db.matcher("forge")(jar("fancyhud-2.0.jar", ["fancyhud"])).source == "learned"
    assert db.check(mod_ids=["fancyhud"]) is not None
    assert db.forget("fancyhud") and db.learned() == []
    assert not db.forget("fancyhud")


@respx.mock
def test_remote_list_used_and_falls_back(settings):
    url = "https://example.com/known-mods.json"
    s = dataclasses.replace(settings, mod_db_url=url)
    respx.get(url).mock(return_value=httpx.Response(200, json={"mods": [{"ids": ["remoteonly"]}], "files": []}))
    db = ModDB(s)
    assert db.check(mod_ids=["remoteonly"]) is not None
    assert db.status()["source"] == "remote"
    # A fresh instance with the URL failing uses the cached copy.
    respx.get(url).mock(return_value=httpx.Response(404))
    db2 = ModDB(s)
    assert db2.shared()[1] == "cache"
    assert db2.check(mod_ids=["remoteonly"]) is not None


@respx.mock
def test_remote_failure_uses_bundled(settings):
    url = "https://example.com/known-mods.json"
    respx.get(url).mock(return_value=httpx.Response(404))
    db = ModDB(dataclasses.replace(settings, mod_db_url=url))
    assert db.shared()[1] == "bundled"
    assert db.check(mod_ids=["oculus"]) is not None


# ---- the build loop -----------------------------------------------------------------


class ScriptedContainer:
    def __init__(self, docker, name, labels, environment, **kwargs):
        self.docker = docker
        self.name = name
        self.labels = labels
        self.env = environment
        self.kwargs = kwargs
        self.status = "created"
        self.exit_code = 0
        self.removed = False
        self.log = ""

    @property
    def attrs(self):
        return {"State": {"Status": self.status, "ExitCode": self.exit_code}}

    def start(self):
        self.status = "running"
        self.docker.behavior(self)

    def reload(self):
        if self.removed:
            raise NotFound("gone")

    def update(self, **kwargs):
        self.kwargs.update(kwargs)

    def stop(self, timeout=10):
        self.status, self.exit_code = "exited", 0

    def remove(self, force=False):
        self.removed = True

    def logs(self, tail=200):
        return self.log.encode()


class ScriptedDocker:
    def __init__(self, behavior):
        self.behavior = behavior
        self.created: list[ScriptedContainer] = []
        outer = self

        class Containers:
            def list(self, all=False, filters=None):
                return [c for c in outer.created if not c.removed]

            def get(self, name):
                for c in reversed(outer.created):
                    if c.name == name and not c.removed:
                        return c
                raise NotFound("no such container")

            def create(self, image, name, labels, environment, **kwargs):
                c = ScriptedContainer(outer, name, labels, environment, image=image, **kwargs)
                outer.created.append(c)
                return c

        class Images:
            pulled = []

            def get(self, name):
                raise ImageNotFound("missing")

            def pull(self, repo, tag):
                self.pulled.append(tag)

        self.containers = Containers()
        self.images = Images()


DONE = '[Server thread/INFO]: Done (12.345s)! For help, type "help"\n'


def pack_behavior(mods_dir):
    """A Forge pack with client-only mods; badmod crashes, then needsbad misses it."""

    def behavior(c):
        files = set(os.listdir(mods_dir)) if os.path.isdir(mods_dir) else set()
        if c.env.get("SETUP_ONLY") == "true":
            os.makedirs(mods_dir, exist_ok=True)
            make_jar(os.path.join(mods_dir, "oculus-1.6.jar"), forge=forge_toml("oculus"))
            make_jar(os.path.join(mods_dir, "clientthing-1.0.jar"), forge=forge_toml("clientthing", client_only=True))
            make_jar(os.path.join(mods_dir, "badmod-1.0.jar"), forge=forge_toml("badmod"), classes=["com.bad.mod.Hud"])
            make_jar(os.path.join(mods_dir, "needsbad-1.0.jar"), forge=forge_toml("needsbad", "Needs Bad"))
            make_jar(os.path.join(mods_dir, "goodmod-1.0.jar"), forge=forge_toml("goodmod"))
            c.status, c.exit_code, c.log = "exited", 0, "[init] Setup only, exiting\n"
        elif "badmod-1.0.jar" in files:
            c.status, c.exit_code = "exited", 1
            c.log = (
                "java.lang.RuntimeException: Attempted to load class net/minecraft/client/Minecraft for invalid dist"
                " DEDICATED_SERVER\n\tat TRANSFORMER/badmod@1.0/com.bad.mod.Hud.init(Hud.java:5)\n"
            )
        elif "needsbad-1.0.jar" in files:
            c.status, c.exit_code = "exited", 1
            c.log = "Mod ID: 'badmod', Requested by: 'needsbad', Expected range: '[1,)', Actual version: '[MISSING]'\n"
        else:
            c.log = DONE

    return behavior


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(server_builder, "POLL_SECONDS", 0)
    monkeypatch.setattr(mod_db_module, "_instance", None)


def build_server(settings, behavior, **req):
    docker = ScriptedDocker(behavior)
    mgr = DockerManager(settings, client=docker)
    base = dict(name="Pack", source="modrinth", project_slug="pack", minecraft_version="1.20.1", loader="forge")
    mgr.create_server(CreateServerRequest(**{**base, **req}), background=False)
    return mgr, docker


def test_build_removes_client_and_crashing_mods(settings):
    mods_dir = os.path.join(settings.data_root, "pack", "mods")
    mgr, docker = build_server(settings, pack_behavior(mods_dir))
    info = mgr.get_server("pack")
    b = info.build
    assert b.phase == "passed", b.message
    assert info.status == "running"
    removed = {m.file: m.reason for m in b.removed}
    assert removed == {
        "oculus-1.6.jar": "known",
        "clientthing-1.0.jar": "client_only",
        "badmod-1.0.jar": "client_only",
        "needsbad-1.0.jar": "missing_dependency",
    }
    assert sorted(os.listdir(mods_dir)) == ["goodmod-1.0.jar"]
    kept = os.listdir(os.path.join(settings.data_root, "pack", ".mcsm", "removed-mods"))
    assert len(kept) == 4
    assert [a.result for a in b.attempts] == ["crashed", "crashed", "started"]

    final = docker.created[-1]
    assert final.kwargs["restart_policy"] == {"Name": "unless-stopped"}
    assert "SETUP_ONLY" not in final.env
    assert "mods/badmod-1.0.jar" in final.env["MODRINTH_IGNORE_MISSING_FILES"]
    assert "badmod-1.0.jar" in final.env["MODRINTH_EXCLUDE_FILES"]
    # Only one container is left; the install and failed test containers were replaced.
    assert len([c for c in docker.created if not c.removed]) == 1
    # The client-only crash was learned for future builds, the pack-specific one wasn't.
    learned = ModDB(settings).learned()
    assert [e["ids"] for e in learned] == [["badmod"]]
    # The report survives in the server folder.
    mgr2 = DockerManager(settings, client=docker)
    assert mgr2.get_build("pack").phase == "passed"


def test_all_client_side_pack_becomes_plain_server(settings):
    mods_dir = os.path.join(settings.data_root, "fo", "mods")

    def behavior(c):
        if c.env.get("SETUP_ONLY") == "true":
            os.makedirs(mods_dir, exist_ok=True)
            make_jar(os.path.join(mods_dir, "sodium.jar"), fabric={"id": "sodium", "environment": "client"})
            make_jar(os.path.join(mods_dir, "zoomer.jar"), fabric={"id": "ok_zoomer"})
            c.status = "exited"
        else:
            c.log = DONE

    mgr, _ = build_server(settings, behavior, name="FO", loader="fabric")
    b = mgr.get_build("fo")
    assert b.phase == "passed"
    assert "plain Fabric server" in b.message
    assert os.listdir(mods_dir) == []


def test_unexplained_crash_fails_with_headline(settings):
    def behavior(c):
        if c.env.get("SETUP_ONLY") == "true":
            c.status = "exited"
        else:
            c.status, c.exit_code = "exited", 1
            c.log = "java.lang.IllegalStateException: something odd\n"

    mgr, docker = build_server(settings, behavior)
    info = mgr.get_server("pack")
    assert info.status == "error"
    assert info.build.phase == "failed"
    assert "IllegalStateException" in info.build.message
    assert docker.created[-1].kwargs["restart_policy"] == {"Name": "no"}


def test_install_failure(settings):
    def behavior(c):
        c.status, c.exit_code, c.log = "exited", 1, "[init] ERROR: Could not resolve modpack version\n"

    mgr, _ = build_server(settings, behavior)
    b = mgr.get_build("pack")
    assert b.phase == "failed" and "Installing the modpack failed" in b.message
    assert b.attempts[0].result == "install_failed"


def test_java_upgrade(settings):
    def behavior(c):
        if c.env.get("SETUP_ONLY") == "true":
            c.status = "exited"
        elif c.kwargs["image"].endswith(":java17"):
            c.status, c.exit_code = "exited", 1
            c.log = (
                "UnsupportedClassVersionError: compiled by a more recent version of the Java Runtime (class file "
                "version 65.0), this version of the Java Runtime only recognizes class file versions up to 61.0\n"
            )
        else:
            c.log = DONE

    mgr, docker = build_server(settings, behavior)
    info = mgr.get_server("pack")
    assert info.build.phase == "passed"
    assert info.java_version == "java21"
    assert "java21" in docker.images.pulled


def test_actions_blocked_while_building(settings):
    mgr = DockerManager(settings, client=ScriptedDocker(lambda c: None))
    from app.models.server import BuildInfo
    from app.services.docker_manager import ServerConflict

    mgr._builds["x"] = BuildInfo(phase="testing")
    with pytest.raises(ServerConflict):
        mgr.start_server("x")


def test_vanilla_skips_builder(settings):
    docker = ScriptedDocker(lambda c: None)
    mgr = DockerManager(settings, client=docker)
    mgr.create_server(CreateServerRequest(name="V", source="vanilla"), background=False)
    assert mgr.get_server("v").build is None
    assert "SETUP_ONLY" not in docker.created[0].env

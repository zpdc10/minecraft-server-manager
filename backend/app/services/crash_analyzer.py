"""Work out which mods made a Minecraft server fail to start.

Input is the container log plus any crash reports, and the scanned jars from the
mods folder. Output is a ranked list of jars to remove, or a non-mod problem
(out of memory, wrong Java version, port in use) that removing mods won't fix.

Evidence is grouped into tiers, strongest first. Only the strongest tier that
names a jar is used, so one clear "client class on a dedicated server" error
doesn't get mixed up with noise from a generic stack trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .mod_scan import ModJar

# Mod ids that belong to the game or loader, never to a removable mod.
CORE_IDS = {
    "minecraft",
    "forge",
    "neoforge",
    "fml",
    "javafml",
    "lowcodefml",
    "fabricloader",
    "fabric-loader",
    "quilt_loader",
    "java",
    "mixinextras",
}
CORE_PACKAGES = (
    "java.",
    "javax.",
    "jdk.",
    "sun.",
    "com.sun.",
    "net.minecraft.",
    "com.mojang.",
    "net.minecraftforge.",
    "net.neoforged.",
    "cpw.mods.",
    "net.fabricmc.",
    "org.quiltmc.",
    "org.spongepowered.",
    "com.llamalad7.mixinextras.",
    "org.apache.",
    "io.netty.",
    "com.google.",
    "it.unimi.",
    "org.objectweb.",
    "org.slf4j.",
    "joptsimple.",
)

# Signs the server came up fine.
DONE_RE = re.compile(r"Done \(\d+[.,]\d+s\)! For help")

_CLIENT_CLASS_RE = re.compile(
    r"(?:invalid dist DEDICATED_SERVER"
    r"|(?:NoClassDefFoundError|ClassNotFoundException):? (?:net[/.]minecraft[/.]client|com[/.]mojang[/.]blaze3d)"
    r"|net/minecraft/client/\S+ (?:is|was) not (?:found|present))",
    re.I,
)
_FRAME_RE = re.compile(r"^\s*at (?:[\w.$-]+/)*?(?:[\w-]+@[\w.+-]+/)?([\w$]+(?:\.[\w$]+)+)\.[\w$<>]+\(")
_TRANSFORMER_RE = re.compile(r"(?:TRANSFORMER|MC-BOOTSTRAP|SECURE-BOOTSTRAP)/([a-z0-9_.-]+)@")
_MOD_FILE_RE = re.compile(r"Mod [Ff]ile: (?:\S*/)?([^/\s]+\.jar)")
_FORGE_FAILED_RE = re.compile(
    r"(?:Failure message|ModLoadingException): .*?\(([a-z0-9_.-]+)\) "
    r"(?:has failed to load correctly|encountered an error)"
)
_FORGE_MISSING_RE = re.compile(r"Mod ID: '([^']+)', Requested by: '([^']+)'.*?Actual version: '?\[?MISSING")
_NEO_MISSING_RE = re.compile(r"Mod (\S+) requires (\S+) [^\n]*\n[^\n]*Currently, (\S+) is not installed")
_FABRIC_MISSING_RE = re.compile(
    r"Mod '[^']*' \(([^)]+)\) \S+ requires .*?(?:mod '[^']*' \(([^)]+)\)|of (\S+?)),? which is missing"
)
_FABRIC_ENTRYPOINT_RE = re.compile(r"provided by '([^']+)'")
_MIXIN_ERROR_RE = re.compile(
    r"MixinApplyError|MixinTransformerError|InvalidMixinException|InvalidInjectionException"
    r"|failed injection check|Mixin apply for mod"
)
_MIXIN_FOR_MOD_RE = re.compile(r"Mixin apply for mod ([\w.-]+) failed")
_MIXIN_CONFIG_RE = re.compile(r"(?:Mixin \[|mixin config |from )([\w.-]+\.json)")
_JAVA_CLASS_RE = re.compile(r"class file version (\d+)\.0\)?, this version of the Java Runtime only recognizes")
_EXCEPTION_START_RE = re.compile(
    r"Exception in server tick loop|Encountered an unexpected exception|---- Minecraft Crash Report ----"
    r"|Failed to start the minecraft server|Exception caught during firing event|Unable to launch|"
    r"Error loading mods|Could not execute entrypoint"
)


@dataclass
class Suspect:
    jar: ModJar
    reason: str  # client_only | missing_dependency | crash
    detail: str


@dataclass
class Analysis:
    suspects: list[Suspect] = field(default_factory=list)
    # A problem removing mods won't fix.
    fatal: str = ""
    # Java major version the mods need, when the log says the current one is too old.
    java_needed: int | None = None
    headline: str = ""


class _Index:
    def __init__(self, jars: list[ModJar]):
        self.by_id: dict[str, ModJar] = {}
        self.by_file: dict[str, ModJar] = {}
        self.by_mixin: dict[str, ModJar] = {}
        self.packages: list[tuple[str, ModJar]] = []
        for j in jars:
            self.by_file[j.filename.lower()] = j
            for mid in j.mod_ids:
                self.by_id.setdefault(mid.lower(), j)
            if j.name:
                self.by_id.setdefault(j.name.lower(), j)
            for m in j.mixin_configs:
                self.by_mixin.setdefault(m.lower(), j)
            for p in j.packages:
                self.packages.append((p + ".", j))
        # Longest package first, so com.foo.bar wins over com.foo.
        self.packages.sort(key=lambda x: -len(x[0]))

    def mod(self, mod_id: str) -> ModJar | None:
        mod_id = mod_id.strip().strip("'\"").lower()
        if not mod_id or mod_id in CORE_IDS:
            return None
        return self.by_id.get(mod_id)

    def for_class(self, cls: str) -> ModJar | None:
        if cls.startswith(CORE_PACKAGES):
            return None
        for prefix, jar in self.packages:
            if cls.startswith(prefix):
                return jar
        return None


def _first_mod_frame(lines: list[str], start: int, idx: _Index, limit: int = 80) -> ModJar | None:
    for line in lines[start : start + limit]:
        m = _TRANSFORMER_RE.search(line)
        if m and (jar := idx.mod(m.group(1))):
            return jar
        m = _FRAME_RE.match(line)
        if m and (jar := idx.for_class(m.group(1))):
            return jar
    return None


def _headline(lines: list[str]) -> str:
    for pat in (r"Description: (.+)", r"(\w+(?:\.\w+)*(?:Exception|Error)\b.*)", r"\b(ERROR|FATAL)\b.*"):
        rx = re.compile(pat)
        for line in lines:
            m = rx.search(line)
            if m and "[init]" not in line:
                return line.strip()[:300]
    return ""


def analyze(text: str, jars: list[ModJar]) -> Analysis:
    idx = _Index(jars)
    lines = text.splitlines()
    result = Analysis(headline=_headline(lines))

    if "java.lang.OutOfMemoryError" in text:
        result.fatal = "The server ran out of memory. Give it more RAM and start it again."
        return result
    if "FAILED TO BIND TO PORT" in text.upper():
        result.fatal = "The server's port is already in use by another program."
        return result
    m = _JAVA_CLASS_RE.search(text)
    if m:
        result.java_needed = int(m.group(1)) - 44

    tiers: list[list[Suspect]] = [[], [], [], []]

    def add(tier: int, jar: ModJar | None, reason: str, detail: str) -> None:
        if jar is not None and all(s.jar is not jar for s in tiers[tier]):
            tiers[tier].append(Suspect(jar, reason, detail.strip()[:300]))

    for i, line in enumerate(lines):
        # Tier 0: a mod touched client-only game code on a dedicated server.
        if _CLIENT_CLASS_RE.search(line):
            jar = None
            for back in lines[max(0, i - 3) : i + 1]:
                mf = _MOD_FILE_RE.search(back)
                if mf:
                    jar = idx.by_file.get(mf.group(1).lower())
            jar = jar or _first_mod_frame(lines, i + 1, idx)
            add(0, jar, "client_only", line)

        # Tier 1: a mod needs another mod that isn't there (often one we removed).
        for mm in _FORGE_MISSING_RE.finditer(line):
            add(1, idx.mod(mm.group(2)), "missing_dependency", f"needs '{mm.group(1)}', which is missing")
        for mm in _FABRIC_MISSING_RE.finditer(line):
            dep = mm.group(2) or mm.group(3) or "?"
            add(1, idx.mod(mm.group(1)), "missing_dependency", f"needs '{dep}', which is missing")

        # Tier 2: the loader named the mod that broke.
        for mm in _FORGE_FAILED_RE.finditer(line):
            add(2, idx.mod(mm.group(1)), "crash", line)
        mm = _MOD_FILE_RE.search(line)
        if mm and i + 1 < len(lines) and "Failure message" in "".join(lines[i : i + 3]):
            add(2, idx.by_file.get(mm.group(1).lower()), "crash", " ".join(lines[i : i + 3]))
        if "Could not execute entrypoint" in line:
            for mm in _FABRIC_ENTRYPOINT_RE.finditer(line):
                add(2, idx.mod(mm.group(1)), "crash", line)
        if _MIXIN_ERROR_RE.search(line):
            mm = _MIXIN_FOR_MOD_RE.search(line)
            if mm:
                add(2, idx.mod(mm.group(1)), "crash", line)
            for mc in _MIXIN_CONFIG_RE.findall(line):
                add(2, idx.by_mixin.get(mc.lower()), "crash", line)

        # Tier 3: the first mod in the crash's stack trace.
        if _EXCEPTION_START_RE.search(line):
            add(3, _first_mod_frame(lines, i + 1, idx, limit=120), "crash", result.headline or line)

    # NeoForge 1.20.5+ spreads the missing dependency message over two lines.
    for mm in _NEO_MISSING_RE.finditer(text):
        add(1, idx.mod(mm.group(1)), "missing_dependency", f"needs '{mm.group(2)}', which is missing")

    for tier in tiers:
        if tier:
            result.suspects = tier[:3]
            break
    return result

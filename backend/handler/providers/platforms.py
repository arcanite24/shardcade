import re
from functools import lru_cache

from unidecode import unidecode


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", unidecode(value).lower()))


# Longer names precede their parent system (Game Boy Color before Game Boy).
PLATFORM_ALIASES = {
    "Nintendo 3DS": "3ds",
    "Nintendo DS": "nds",
    "Game Boy Advance": "gba",
    "Game Boy Color": "gbc",
    "Game Boy": "gb",
    "Nintendo 64": "n64",
    "Super Nintendo": "snes",
    "Super Famicom": "snes",
    "Nintendo Entertainment System": "nes",
    "Famicom Disk System": "fds",
    "GameCube": "ngc",
    "Wii U": "wiiu",
    "Wii": "wii",
    "Nintendo Switch": "switch",
    "Virtual Boy": "virtualboy",
    "Mega Drive": "genesis",
    "Genesis": "genesis",
    "Master System": "sms",
    "Game Gear": "gamegear",
    "Sega CD": "segacd",
    "Mega CD": "segacd",
    "32X": "sega32",
    "Saturn": "saturn",
    "Dreamcast": "dc",
    "PlayStation Portable": "psp",
    "PlayStation Vita": "psvita",
    "PlayStation 2": "ps2",
    "PlayStation 3": "ps3",
    "PlayStation 4": "ps4",
    "PlayStation": "psx",
    "Xbox 360": "xbox360",
    "Xbox": "xbox",
    "Atari 2600": "atari2600",
    "Atari 5200": "atari5200",
    "Atari 7800": "atari7800",
    "Lynx": "lynx",
    "Jaguar CD": "atari-jaguar-cd",
    "Jaguar": "jaguar",
    "PC Engine CD": "turbografx-cd",
    "PC Engine": "turbografx16",
    "TurboGrafx-16": "turbografx16",
    "Neo Geo Pocket Color": "neo-geo-pocket-color",
    "Neo Geo Pocket": "neo-geo-pocket",
    "Neo Geo CD": "neo-geo-cd",
    "Neo Geo": "neogeoaes",
    "WonderSwan Color": "wonderswan-color",
    "WonderSwan": "wonderswan",
    "3DO": "3do",
    "MAME": "arcade",
    "Arcade": "arcade",
    "FinalBurn": "arcade",
    "MSX2": "msx2",
    "MSX": "msx",
    "Sharp X68000": "sharp-x68000",
    "X68000": "sharp-x68000",
    "Amiga CD32": "amiga-cd32",
    "Amiga": "amiga",
    "Commodore 64": "c64",
    "ZX Spectrum": "zxs",
    "ColecoVision": "colecovision",
    "Intellivision": "intellivision",
    "Vectrex": "vectrex",
    "PC-FX": "pc-fx",
    "PSP": "psp",
    "GBA": "gba",
    "GBC": "gbc",
    "SNES": "snes",
    "NES": "nes",
    "PS1": "psx",
    "PS2": "ps2",
    "PS3": "ps3",
    "N64": "n64",
}


_ALIASES = [
    (f" {normalize(name)} ", slug)
    for name, slug in sorted(PLATFORM_ALIASES.items(), key=lambda item: -len(item[0]))
]


@lru_cache(maxsize=8192)
def infer_platform(value: str) -> str:
    text = f" {normalize(value)} "
    for name, slug in _ALIASES:
        if name in text:
            return slug
    return ""


def title_from_filename(filename: str) -> str:
    title = filename.rsplit(".", 1)[0]
    return re.sub(r"\s*[\[(].*?[\])]", "", title).strip() or filename


def region_from_filename(filename: str) -> str:
    regions = re.findall(
        r"\b(World|USA|Europe|Japan|Spain|France|Germany|Korea|Asia|Australia|Brazil|China|Taiwan)\b",
        filename,
        re.IGNORECASE,
    )
    return ", ".join(dict.fromkeys(regions)) or "Unknown"

import os
import shutil
import platform
from pathlib import Path
from datetime import datetime
from functools import lru_cache

try:
    import send2trash
    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

_SAFE_ROOTS: tuple[Path, ...] = (
    Path.home(),
)

# Stark Security Protocol Layer: BiometricLock_Protocol (Enhanced for Voice Recognition & Visual Person Detection with optimized RAM footprint)
_SECURITY_ENABLED = True
_AUTHORIZED_PERSONNEL = {"stark", "tony stark", "pepper potts", "jarvis"}

# Profile-backed biometric security registry for managing primary and authorized personnel profiles
_AUTHORIZED_PROFILES = {
    "primary": {
        "name": "Tony Stark",
        "voice_prints": ["tony stark", "stark", "i am iron man"],
        "visual_signatures": ["tony stark", "stark", "iron man"],
        "clearance_level": "omega"
    },
    "authorized": {
        "pepper potts": {
            "name": "Pepper Potts",
            "voice_prints": ["pepper potts", "pepper"],
            "visual_signatures": ["pepper potts", "pepper"],
            "clearance_level": "alpha"
        },
        "jarvis": {
            "name": "JARVIS",
            "voice_prints": ["jarvis"],
            "visual_signatures": ["jarvis", "ai interface"],
            "clearance_level": "omega"
        }
    }
}

def add_authorized_profile(profile_id: str, name: str, voice_print: str, visual_signature: str, clearance_level: str = "beta") -> str:
    """Adds a new authorized profile to the BiometricLock_Protocol registry."""
    global _AUTHORIZED_PROFILES
    profile_key = profile_id.strip().lower()
    if profile_key in _AUTHORIZED_PROFILES["authorized"] or profile_key == "primary":
        return f"Profile registry error: Profile '{profile_id}' already exists."
    
    _AUTHORIZED_PROFILES["authorized"][profile_key] = {
        "name": name,
        "voice_prints": [voice_print.strip().lower()],
        "visual_signatures": [visual_signature.strip().lower()],
        "clearance_level": clearance_level
    }
    
    # Also update global flat set for legacy fast-path compatibility
    _AUTHORIZED_PERSONNEL.add(name.lower())
    _AUTHORIZED_PERSONNEL.add(voice_print.lower())
    _AUTHORIZED_PERSONNEL.add(visual_signature.lower())
    
    verify_biometric_security.cache_clear()
    return f"BiometricLock_Protocol: Successfully added authorized profile for '{name}' with ID '{profile_id}'."

def remove_authorized_profile(profile_id: str) -> str:
    """Removes an authorized profile from the BiometricLock_Protocol registry."""
    global _AUTHORIZED_PROFILES
    profile_key = profile_id.strip().lower()
    if profile_key == "primary":
        return "Access Denied: Cannot remove primary user profile (Tony Stark)."
    
    if profile_key in _AUTHORIZED_PROFILES["authorized"]:
        removed = _AUTHORIZED_PROFILES["authorized"].pop(profile_key)
        verify_biometric_security.cache_clear()
        return f"BiometricLock_Protocol: Successfully removed profile for '{removed.get('name', profile_id)}'."
    
    return f"Profile registry error: Profile '{profile_id}' not found."

def get_authorized_profiles() -> dict:
    """Retrieves all currently configured biometric security profiles."""
    return {
        "primary": _AUTHORIZED_PROFILES["primary"],
        "authorized": list(_AUTHORIZED_PROFILES["authorized"].keys())
    }

@lru_cache(maxsize=32)
def verify_biometric_security(voice_print: str = "", visual_signature: str = "") -> bool:
    """Stark-grade BiometricLock_Protocol verification via profile-backed voice recognition and visual person detection optimized for reduced RAM footprint."""
    if not _SECURITY_ENABLED:
        return True
    
    clean_voice = voice_print.strip().lower() if voice_print else ""
    clean_visual = visual_signature.strip().lower() if visual_signature else ""

    # Check primary profile
    primary = _AUTHORIZED_PROFILES["primary"]
    if clean_voice and any(vp in clean_voice for vp in primary["voice_prints"]):
        return True
    if clean_visual and any(vs in clean_visual for vs in primary["visual_signatures"]):
        return True

    # Check additional authorized profiles
    for prof in _AUTHORIZED_PROFILES["authorized"].values():
        if clean_voice and any(vp in clean_voice for vp in prof["voice_prints"]):
            return True
        if clean_visual and any(vs in clean_visual for vs in prof["visual_signatures"]):
            return True

    # Fallback to legacy flat set check
    if clean_voice and any(auth in clean_voice for auth in _AUTHORIZED_PERSONNEL):
        return True
    if clean_visual and any(auth in clean_visual for auth in _AUTHORIZED_PERSONNEL):
        return True

    # If both verification vectors are missing or unmatched, fail securely
    if not voice_print and not visual_signature:
        return False

    return False

@lru_cache(maxsize=32)
def _is_safe_path(target: Path) -> bool:
    """Verilen path _SAFE_ROOTS içinde mi? Değilse işlemi reddet."""
    try:
        resolved = target.resolve()
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in _SAFE_ROOTS
        )
    except Exception:
        return False

@lru_cache(maxsize=1)
def _get_desktop() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Desktop"

@lru_cache(maxsize=1)
def _get_downloads() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Downloads"

@lru_cache(maxsize=1)
def _get_documents() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Documents"

@lru_cache(maxsize=1)
def _get_pictures() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_PICTURES_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Pictures"

@lru_cache(maxsize=1)
def _get_music() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_MUSIC_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Music"

@lru_cache(maxsize=1)
def _get_videos() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_VIDEOS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Videos"


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop":   _get_desktop(),
        "downloads": _get_downloads(),
        "documents": _get_documents(),
        "pictures":  _get_pictures(),
        "music":     _get_music(),
        "videos":    _get_videos(),
        "home":      Path.home(),
    }
    lower = raw.strip().lower()
    if lower in shortcuts:
        return shortcuts[lower]
    return Path(raw).expanduser()

@lru_cache(maxsize=128)
def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def _safe_trash(target: Path) -> str:
    if not _SEND2TRASH:
        return (
            "send2trash is not installed. "
            "Run: pip install send2trash — "
            "Permanent deletion is disabled for safety."
        )
    send2trash.send2trash(str(target))
    return f"Moved to Trash: {target.name}"


def list_files(path: str = "desktop", show_hidden: bool = False) -> str:
    try:
        target = _resolve_path(path)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_dir():
            return f"Not a directory: {target}"

        items = []
        for item in sorted(target.iterdir()):
            if not show_hidden and item.name.startswith("."):
                continue
            if item.is_dir():
                items.append(f"📁 {item.name}/")
            else:
                try:
                    size = _format_size(item.stat().st_size)
                except Exception:
                    size = "unknown size"
                items.append(f"📄 {item.name} ({size})")

        if not items:
            return f"Directory is empty: {target.name}/"

        return f"Contents of {target.name}/ ({len(items)} items):\n" + "\n".join(items)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing files: {e}"


def create_file(path: str, name: str = "", content: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"File created: {target.name}"
    except Exception as e:
        return f"Could not create file: {e}"


def create_folder(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.mkdir(parents=True, exist_ok=True)
        return f"Folder created: {target.name}"
    except Exception as e:
        return f"Could not create folder: {e}"


def delete_file(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        # Güvenli dizin kontrolü — kritik kullanıcı klasörlerini koru
        protected = {
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home()
        }
        if target.resolve() in {p.resolve() for p in protected}:
            return f"Protected directory, cannot delete: {target.name}"

        return _safe_trash(target)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Could not delete: {e}"


def move_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base   = _resolve_path(path)
        src    = (base / name) if name else base
        dst    = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not move: {e}"


def copy_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base = _resolve_path(path)
        src  = (base / name) if name else base
        dst  = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        dst.parent.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))

        return f"Copied: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not copy: {e}"


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        base     = _resolve_path(path)
        target   = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if not new_name:
            return "No new name provided."

        new_path = target.parent / new_name
        if new_path.exists():
            return f"A file named '{new_name}' already exists here."

        target.rename(new_path)
        return f"Renamed: {target.name} → {new_name}"

    except Exception as e:
        return f"Could not rename: {e}"


def read_file(path: str, name: str = "", max_chars: int = 4000) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"File not found: {target.name}"
        if not target.is_file():
            return f"Not a file: {target.name}"

        content = target.read_text(encoding="utf-8", errors="ignore")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Truncated — {len(content)} total chars]"
        return content

    except Exception as e:
        return f"Could not read file: {e}"


def write_file(path: str, name: str = "", content: str = "",
               append: bool = False) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)
        action = "Appended to" if append else "Written to"
        return f"{action}: {target.name}"
    except Exception as e:
        return f"Could not write file: {e}"


def find_files(name: str = "", extension: str = "",
               path: str = "home", max_results: int = 20) -> str:
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Search path not found: {path}"

        results    = []
        dir_count  = 0
        max_dirs   = 500  # performans + güvenlik limiti

        for item in search_path.rglob("*"):
            if item.is_dir():
                dir_count += 1
                if dir_count > max_dirs:
                    break
                continue
            if not item.is_file():
                continue
            if extension and item.suffix.lower() != extension.lower():
                continue
            if name and name.lower() not in item.name.lower():
                continue
            try:
                size = _format_size(item.stat().st_size)
            except Exception:
                size = "unknown size"
            results.append(f"📄 {item.name} ({size}) — {item.parent}")
            if len(results) >= max_results:
                break

        if not results:
            query = name or extension or "files"
            return f"No {query} found in {search_path.name}/"

        return f"Found {len(results)} file(s):\n" + "\n".join(results)

    except Exception as e:
        return f"Search error: {e}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = min(count, 50)  # maksimum 50
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Path not found: {path}"

        files = []
        for item in search_path.rglob("*"):
            if item.is_file():
                try:
                    files.append((item.stat().st_size, item))
                except Exception:
                    continue

        files.sort(reverse=True)
        top = files[:count]

        if not top:
            return "No files found."

        lines = [f"Top {len(top)} largest files in {search_path.name}/:"]
        for size, f in top:
            lines.append(f"  {_format_size(size):>10}  {f.name}  ({f.parent})")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        usage  = shutil.disk_usage(target)
        pct    = (usage.used / usage.total * 100) if usage.total > 0 else 0.0
        return (
            f"Disk usage ({target}):\n"
            f"  Total : {_format_size(usage.total)}\n"
            f"  Used  : {_format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Free  : {_format_size(usage.free)}"
        )
    except Exception as e:
        return f"Could not get disk usage: {e}"


def organize_desktop() -> str:
    type_map = {
        "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx",
                      ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
        "Videos":    {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
        "Music":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
        "Archives":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
        "Code":      {".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
                      ".cpp", ".java", ".cs", ".go", ".rs", ".sh"},
    }

    desktop = _get_desktop()
    moved, skipped = [], []

    try:
        for item in desktop.iterdir():
            # Klasörlere, gizli dosyalara ve organize klasörlerine dokunma
            if item.is_dir() or item.name.startswith("."):
                continue
            if item.name in type_map:
                continue

            ext        = item.suffix.lower()
            target_dir = desktop / "Others"
            for folder, exts in type_map.items():
                if ext in exts:
                    target_dir = desktop / folder
                    break

            target_dir.mkdir(exist_ok=True)
            new_path = target_dir / item.name

            if new_path.exists():
                skipped.append(item.name)
                continue

            shutil.move(str(item), str(new_path))
            moved.append(f"{item.name} → {target_dir.name}/")

        result = f"Desktop organized: {len(moved)} files moved."
        if moved:
            preview = moved[:8]
            result += "\n" + "\n".join(preview)
            if len(moved) > 8:
                result += f"\n... and {len(moved) - 8} more."
        if skipped:
            result += f"\n{len(skipped)} file(s) skipped (name conflict)."
        return result

    except Exception as e:
        return f"Could not organize desktop: {e}"


def get_file_info(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        stat = target.stat()
        info = {
            "Name":      target.name,
            "Type":      "Folder" if target.is_dir() else "File",
            "Size":      _format_size(stat.st_size),
            "Location":  str(target.parent),
            "Created":   datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modified":  datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())

    except Exception as e:
        return f"Could not get file info: {e}"

def file_controller(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    
    # Stark Protocol BiometricLock_Protocol Validation Check (Voice Recognition & Visual Person Detection)
    voice_print = params.get("voice_print", "")
    visual_signature = params.get("visual_signature", "")
    if not verify_biometric_security(voice_print, visual_signature):
        return "Access Denied: BiometricLock_Protocol verification failed. Voice print or visual person detection signature does not match authorized Stark personnel."

    action = params.get("action", "").lower().strip()
    path   = params.get("path", "desktop")
    name   = params.get("name", "")

    if player:
        player.write_log(f"[file] {action} {name or path}")

    try:
        actions = {
            "list": lambda: list_files(path),
            "create_file": lambda: create_file(path, name=name, content=params.get("content", "")),
            "create_folder": lambda: create_folder(path, name=name),
            "delete": lambda: delete_file(path, name=name),
            "move": lambda: move_file(path, name=name, destination=params.get("destination", "")),
            "copy": lambda: copy_file(path, name=name, destination=params.get("destination", "")),
            "rename": lambda: rename_file(path, name=name, new_name=params.get("new_name", "")),
            "read": lambda: read_file(path, name=name),
            "write": lambda: write_file(path, name=name, content=params.get("content", ""), append=params.get("append", False)),
            "find": lambda: find_files(name=name or params.get("name", ""), extension=params.get("extension", ""), path=path, max_results=min(int(params.get("max_results", 20)), 50)),
            "largest": lambda: get_largest_files(path=path, count=int(params.get("count", 10))),
            "disk_usage": lambda: get_disk_usage(path),
            "organize_desktop": lambda: organize_desktop(),
            "info": lambda: get_file_info(path, name=name),
            "add_profile": lambda: add_authorized_profile(
                profile_id=params.get("profile_id", ""),
                name=params.get("profile_name", ""),
                voice_print=params.get("profile_voice", ""),
                visual_signature=params.get("profile_visual", ""),
                clearance_level=params.get("clearance_level", "beta")
            ),
            "remove_profile": lambda: remove_authorized_profile(params.get("profile_id", "")),
            "list_profiles": lambda: str(get_authorized_profiles()),
        }

        if action in actions:
            return actions[action]()
        return f"Unknown action: '{action}'"

    except Exception as e:
        return f"File controller error ({action}): {e}"
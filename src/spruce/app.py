#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Spruce — GNOME Cleaner

from __future__ import annotations

import os
import re
import math
import shutil
from pathlib import Path
from typing import List, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango, PangoCairo  # type: ignore

try:
    import cairo  # type: ignore
except Exception:
    cairo = None

APP_ID = "io.github.shonubot.Spruce"
IS_FLATPAK = os.environ.get("FLATPAK_ID") == APP_ID
SPRUCE_DEBUG = os.environ.get("SPRUCE_DEBUG") == "1"


# ─────────────────────────── helpers ───────────────────────────

def _find_ui() -> str:
    here = Path(__file__).resolve()
    for p in [
        here.parent.parent / "ui" / "window.ui",
        Path.cwd() / "ui" / "window.ui",
        Path("/app/ui/window.ui"),
        Path("/app/share/io.github.shonubot.Spruce/ui/window.ui"),
        Path("/app/share/spruce/ui/window.ui"),
    ]:
        if p.exists():
            return str(p)
    return "ui/window.ui"


def human_size(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024.0
    return f"{f:.1f}EiB"


def _gio_fs_usage(path: Path) -> Tuple[int, int, int] | None:
    try:
        info = Gio.File.new_for_path(str(path)).query_filesystem_info(
            "filesystem::size,filesystem::free", None
        )
        total = int(info.get_attribute_uint64("filesystem::size"))
        free = int(info.get_attribute_uint64("filesystem::free"))
        used = max(0, total - free)
        if total > 0 and free >= 0 and used >= 0:
            return total, used, free
    except Exception:
        pass
    return None


def disk_usage_home() -> Tuple[int, int, int]:
    p = Path.home()
    ans = _gio_fs_usage(p)
    if ans:
        return ans
    try:
        total, used, free = shutil.disk_usage(str(p))
        if total > 0:
            return int(total), int(used), int(free)
    except Exception:
        pass
    return 1, 0, 1


def xdg_cache() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))


# ─────────────────────────── subprocess helpers (Gio) ───────────────────────────

def _host_exec(*argv: str) -> list[str]:
    return ["flatpak-spawn", "--host", *argv] if IS_FLATPAK else list(argv)

def _run(argv: list[str], stdin_text: str | None = None) -> tuple[int, str, str]:
    """Run a command with Gio.Subprocess and capture stdout/stderr (UTF-8)."""
    try:
        flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        if stdin_text is not None:
            flags |= Gio.SubprocessFlags.STDIN_PIPE
        sp = Gio.Subprocess.new(argv, flags)
        ok, out, err = sp.communicate_utf8(stdin_text, None)
        if ok:
            return 0, out or "", err or ""
        try:
            code = sp.get_exit_status()
        except Exception:
            code = 1
        return code, out or "", err or ""
    except Exception as e:
        return 127, "", str(e)


# ─────────────────── diagnostics to UI (debug-only) ───────────────────

def _append_diag(win: Gtk.Widget | None, lines: list[str]) -> None:
    if not SPRUCE_DEBUG:
        return  # quiet unless explicitly enabled
    try:
        app = Gtk.Application.get_default()
        w = (app.props.active_window if app else None) or win  # type: ignore
        if not w or not hasattr(w, "pkg_list"):
            return
        lbl: Gtk.Label = getattr(w, "pkg_list")  # type: ignore
        cur = lbl.get_text()
        text = "\n".join(lines)
        lbl.set_text((cur + "\n" if cur else "") + text)
    except Exception:
        pass


# ─────────────────────────── flatpak logic ───────────────────────────

APP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_.-]+)+$")
RUNTIME_LINE_RE = re.compile(r"^Runtime:\s*(.+?)\s*$", re.IGNORECASE)

def _host_list_apps(scope: str) -> list[str]:
    # Prefer columns API
    code, out, _ = _run(_host_exec("flatpak", "list", "--app", scope, "--columns=application"))
    apps: list[str] = []
    if code == 0 and out.strip():
        for ln in out.splitlines():
            app = ln.strip()
            if APP_ID_RE.match(app):
                apps.append(app)
    # Fallback parse
    if not apps:
        code2, out2, err2 = _run(_host_exec("flatpak", "list", "--app", scope))
        text = out2 if code2 == 0 else err2
        for ln in text.splitlines():
            for tok in ln.split():
                if APP_ID_RE.match(tok):
                    apps.append(tok); break
    return apps


def _host_runtime_of_app(app_id: str, scope: str) -> str | None:
    code, out, err = _run(_host_exec("flatpak", "info", app_id, scope))
    text = out if code == 0 else err
    for ln in text.splitlines():
        m = RUNTIME_LINE_RE.match(ln)
        if m:
            r = m.group(1).strip()
            return r if r.startswith("runtime/") else f"runtime/{r}"
    return None


def _list_runtime_refs_via_flatpak(scope: str) -> list[str]:
    """Prefer authoritative list from Flatpak itself."""
    code, out, _ = _run(_host_exec("flatpak", "list", "--runtime", scope, "--columns=ref"))
    refs: list[str] = []
    if code == 0 and out.strip():
        refs = [ln.strip() for ln in out.splitlines() if ln.strip()]
        refs = [r if r.startswith("runtime/") else f"runtime/{r}" for r in refs]
        return sorted(set(refs))

    # Older fallback: parse table output
    code2, out2, err2 = _run(_host_exec("flatpak", "list", "--runtime", scope))
    text = out2 if code2 == 0 else err2
    for ln in text.splitlines():
        toks = [t for t in ln.split() if "/" in t]
        if toks:
            t = toks[-1].strip()
            if t.count("/") >= 2:
                refs.append(t if t.startswith("runtime/") else f"runtime/{t}")
    return sorted(set(refs))


def _host_installed_runtime_refs(scope: str) -> list[str]:
    """Get installed runtime refs for --user or --system."""
    # 1) Ask flatpak
    refs = _list_runtime_refs_via_flatpak(scope)
    if refs:
        return [r for r in refs if r.startswith("runtime/")]

    # 2) Pure-Python fallback: walk runtime dirs
    roots: list[Path] = []
    if scope == "--user":
        roots.append(Path.home() / ".local" / "share" / "flatpak" / "runtime")
        vhome = Path("/var/home") / os.environ.get("USER", "")
        roots.append(vhome / ".local" / "share" / "flatpak" / "runtime")
    else:
        roots.append(Path("/var/lib/flatpak/runtime"))

    results: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for name_dir in root.iterdir():
                if not name_dir.is_dir():
                    continue
                for arch_dir in name_dir.iterdir():
                    if not arch_dir.is_dir():
                        continue
                    for br_dir in arch_dir.iterdir():
                        if not br_dir.is_dir():
                            continue
                        name, arch, br = name_dir.name, arch_dir.name, br_dir.name
                        results.add(f"runtime/{name}/{arch}/{br}")
        except Exception:
            pass

    return sorted(results)


# Safety settings
_ALWAYS_KEEP_EXT_SUFFIXES = (".GL.default", ".codecs", ".codecs-extra", ".openh264")

def _is_always_kept_extension(ref: str) -> bool:
    name = _base_of(ref)[0]
    return any(name.endswith(suf) for suf in _ALWAYS_KEEP_EXT_SUFFIXES)

def _is_sdk_family(ref: str) -> bool:
    name = _base_of(ref)[0]
    return name.endswith(".Sdk") or name.endswith(".Sdk.Locale")

def _is_platform_family(ref: str) -> bool:
    name = _base_of(ref)[0]
    return (
        name.endswith(".Platform")
        or name.endswith(".Platform.Locale")
        or name.endswith(".Platform.Debug")
    )

def _base_of(ref: str) -> tuple[str, str, str]:
    parts = ref.split("/", 4)
    return (parts[1], parts[2], parts[3]) if len(parts) >= 4 else (ref, "", "")

def _is_base_runtime(ref: str) -> bool:
    name, _, _ = _base_of(ref)
    if name.endswith(".Locale") or name.endswith(".Debug"):
        return False
    if any(name.endswith(s) for s in (".GL.default", ".ffmpeg-full", ".openh264", ".codecs", ".codecs-extra")):
        return False
    return True

def _platform_from_ext(ref: str) -> str:
    name, _, _ = _base_of(ref)
    parts = name.split(".")
    return ".".join(parts[:3]) if len(parts) >= 3 else name

def _sdk_to_platform_name(sdk_name: str) -> str:
    # org.gnome.Sdk -> org.gnome.Platform (keep same first 3 components)
    parts = sdk_name.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:3]) + ".Platform"
    return sdk_name.replace(".Sdk", ".Platform")


_PIN_COMMENT_RE = re.compile(r"#.*$")

def _list_pins(scope: str) -> set[str]:
    """
    Return pinned refs for --user/--system.
    Prefer `flatpak pin --list`; otherwise parse pinned/ files in pure Python.
    """
    pins: set[str] = set()

    # 1) Try newer CLI
    code, out, _ = _run(_host_exec("flatpak", "pin", "--list", scope))
    if code == 0 and out.strip():
        for ln in out.splitlines():
            ref = ln.strip()
            if ref:
                pins.add(ref if ref.startswith("runtime/") else f"runtime/{ref}")
        return pins

    # 2) Pure-Python fallback
    pin_dir = (Path.home() / ".local/share/flatpak/pinned"
               if scope == "--user" else Path("/var/lib/flatpak/pinned"))
    if not pin_dir.is_dir():
        return pins

    # A) file names that look like refs
    try:
        for p in sorted(pin_dir.iterdir(), key=lambda q: q.name.lower()):
            bn = p.name
            looks_like_ref = (
                bn.count("/") >= 2 or re.match(
                    r'^[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_.-]+)+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', bn
                )
            )
            if looks_like_ref:
                pins.add(bn if bn.startswith("runtime/") else f"runtime/{bn}")
    except Exception:
        pass

    # B) file contents (strip comments/whitespace; skip blank)
    try:
        for p in sorted(pin_dir.iterdir(), key=lambda q: q.name.lower()):
            if not p.is_file():
                continue
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = _PIN_COMMENT_RE.sub("", raw).strip()
                    if not line:
                        continue
                    if not line.startswith("runtime/"):
                        if re.match(
                            r'^[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_.-]+)+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', line
                        ):
                            line = f"runtime/{line}"
                        else:
                            continue
                    pins.add(line)
    except Exception:
        pass

    return pins


def _installed_sdk_refs(scope: str) -> list[str]:
    """Return installed SDK refs (and Sdk.Locale) for scope."""
    refs = _host_installed_runtime_refs(scope)
    sdks = [r for r in refs if _is_sdk_family(r)]
    return sdks


def list_flatpak_unused_with_diag(win: Gtk.Widget) -> tuple[list[str], list[str], list[str]]:
    """
    Return (removable_refs, pinned_refs, kept_refs) after applying rules.
    - pinned_refs: anything pinned (shown, not removable)
    - kept_refs: safety-kept items (Platform/Locale, always-kept extensions, SDKs)
    """
    diag: list[str] = []
    removable_all: list[str] = []
    pinned_all: list[str] = []
    kept_all: list[str] = []

    for scope in ("--user", "--system"):
        refs = _host_installed_runtime_refs(scope)
        apps = _host_list_apps(scope)
        pins = _list_pins(scope)
        sdks = _installed_sdk_refs(scope)

        diag.append(f"{scope} installed: {len(refs)}")
        if SPRUCE_DEBUG:
            diag.append(f"{scope} refs: {refs}")
            diag.append(f"{scope} pins: {sorted(pins)}")
            diag.append(f"{scope} sdks: {sdks}")
        diag.append(f"{scope} apps: {apps}")

        if not refs:
            continue

        # Determine platform bases used by apps (and implied by SDKs)
        used_platform_bases = set()
        for a in apps:
            r = _host_runtime_of_app(a, scope)
            if r:
                used_platform_bases.add(_base_of(r)[0])
        for sdk_ref in sdks:
            sdk_name, arch, br = _base_of(sdk_ref)
            platform_name = _sdk_to_platform_name(sdk_name)
            used_platform_bases.add(platform_name)

        if SPRUCE_DEBUG:
            diag.append(f"{scope} used platform bases: {sorted(used_platform_bases)}")

        for ref in refs:
            # First: if pinned, surface in pinned list (regardless of safety filters)
            if ref in pins:
                pinned_all.append(ref)
                continue

            # Now compute safety status and “unused by our rules”
            is_sdk = _is_sdk_family(ref)
            is_platform = _is_platform_family(ref)
            is_always_kept = _is_always_kept_extension(ref)

            # Decide the base name for extension matching
            base_name = _base_of(ref)[0] if _is_base_runtime(ref) else _platform_from_ext(ref)

            # If this runtime/extension is actually used by an app/SDK-implied platform, skip entirely
            if base_name in used_platform_bases:
                continue

            # If it’s one of the safety-kept types, list it under “Kept for safety”
            if is_sdk or is_platform or is_always_kept:
                kept_all.append(ref)
                continue

            # Otherwise it’s removable by our rules
            removable_all.append(ref)

    # de-dup while preserving order
    def dedup(seq: list[str]) -> list[str]:
        seen = set(); out = []
        for r in seq:
            if r not in seen:
                seen.add(r); out.append(r)
        return out

    removable = dedup(removable_all)
    pinned = dedup(pinned_all)
    kept = dedup(kept_all)

    diag.append(f"unused refs (removable): {removable}")
    diag.append(f"unused refs (pinned): {pinned}")
    diag.append(f"unused refs (kept): {kept}")
    _append_diag(win, diag)
    return removable, pinned, kept


def run_flatpak_autoremove_async(on_done) -> None:
    """Run flatpak uninstall --unused for user+system and toast the result."""
    def _run_and_count(scope: str) -> int:
        code, out, err = _run(_host_exec("flatpak", "uninstall", "--unused", scope, "-y"))
        return sum(1 for ln in (out or "").splitlines() if ln.strip().startswith("Uninstalling "))

    removed = 0
    try: removed += _run_and_count("--user")
    except Exception: pass
    try: removed += _run_and_count("--system")
    except Exception: pass

    def _after():
        on_done()
        app = Gtk.Application.get_default()
        win = app.props.active_window if app else None
        if win and hasattr(win, "_toast"):
            try:
                win._toast(f"Removed {removed} item(s)" if removed > 0
                           else "Flatpak reported nothing unused to uninstall")
            except Exception:
                pass
        return GLib.SOURCE_REMOVE

    GLib.timeout_add_seconds(1, _after)


# ─────────────────────────── cache sweep ───────────────────────────

def dir_size(path: Path) -> int:
    total = 0
    if path.is_dir():
        for root, _dirs, files in os.walk(path, onerror=lambda *_: None):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except Exception:
                    pass
    elif path.exists():
        try:
            total += path.stat().st_size
        except Exception:
            pass
    return total


def _du_host_bytes(path: str) -> int:
    p = Path(path)
    total = 0
    try:
        if p.is_dir():
            for root, _dirs, files in os.walk(p, onerror=lambda *_: None):
                for f in files:
                    try:
                        total += (Path(root) / f).stat().st_size
                    except Exception:
                        pass
        elif p.exists():
            total = p.stat().st_size
    except Exception:
        total = 0
    return total


def _host_first_level_cache_entries() -> list[tuple[str, int]]:
    """First-level children of $HOME/.cache with sizes (pure Python)."""
    root = Path.home() / ".cache"
    if not root.is_dir():
        return []
    entries: list[tuple[str, int]] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            entries.append((str(child), _du_host_bytes(str(child))))
    except Exception:
        pass
    entries.sort(key=lambda t: t[1], reverse=True)
    return entries


def _host_app_cache_entries() -> list[tuple[str, int]]:
    """Top-level Flatpak app caches (~/.var/app/*/cache) on host (pure Python)."""
    base = Path.home() / ".var" / "app"
    if not base.is_dir():
        return []
    out: list[tuple[str, int]] = []
    try:
        for appdir in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            c = appdir / "cache"
            if c.is_dir():
                out.append((str(c), _du_host_bytes(str(c))))
    except Exception:
        pass
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def _sandbox_first_level_cache_entries() -> list[tuple[Path, int]]:
    """First-level children of sandbox XDG_CACHE_HOME with sizes."""
    root = xdg_cache()
    result: list[tuple[Path, int]] = []
    if not root.is_dir():
        return result
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        try:
            result.append((child, dir_size(child)))
        except Exception:
            pass
    result.sort(key=lambda t: t[1], reverse=True)
    return result


def _host_cache_paths_and_sizes() -> list[tuple[str, int]]:
    """Compatibility shim: host ~/.cache/* + ~/.var/app/*/cache."""
    return _host_first_level_cache_entries() + _host_app_cache_entries()


# ─────────────────────────── deletion guard ───────────────────────────

_ALLOWED_HOST_PREFIXES = [
    Path.home() / ".cache",
    Path.home() / ".var" / "app",
]

def _is_allowed_host_target(p: Path) -> bool:
    try:
        rp = p.resolve()
        for pref in _ALLOWED_HOST_PREFIXES:
            if rp.is_relative_to(pref.resolve()):
                return True
    except Exception:
        pass
    return False


# ─────────────────────────── UI ───────────────────────────

@Gtk.Template(filename=_find_ui())
class SpruceWindow(Adw.ApplicationWindow):
    __gtype_name__ = "SpruceWindow"

    pie_chart: Gtk.DrawingArea = Gtk.Template.Child("pie_chart")
    clear_btn: Gtk.Button = Gtk.Template.Child("clear_btn")
    options_btn: Gtk.Button = Gtk.Template.Child("options_btn")
    pkg_list: Gtk.Label = Gtk.Template.Child("pkg_list")
    remove_btn: Gtk.Button = Gtk.Template.Child("remove_btn")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Chart
        self.pie_chart.set_hexpand(True)
        self.pie_chart.set_vexpand(True)
        self.pie_chart.set_content_width(360)
        self.pie_chart.set_content_height(260)
        self.pie_chart.set_draw_func(self._draw_chart, None)

        # Buttons
        self.clear_btn.connect("clicked", self._on_clear_clicked)
        self.options_btn.connect("clicked", self._on_options_clicked)
        self.remove_btn.connect("clicked", self._on_remove_clicked)
        self.timeout_source = None

        # Add a small "What's hidden?" button next to Remove
        self.kept_btn = Gtk.Button(label="What’s hidden?")
        self.kept_btn.set_has_frame(True)
        self.kept_btn.set_valign(Gtk.Align.CENTER)
        self.kept_btn.set_sensitive(False)
        self.kept_btn.connect("clicked", self._on_show_kept_clicked)
        try:
            parent = self.remove_btn.get_parent()
            if isinstance(parent, Gtk.Box):
                parent.append(self.kept_btn)
        except Exception:
            pass

        # Options
        self._opts = {"thumbs": True, "webkit": True, "fontconf": True, "mesa": True, "sweep": True}
        self._current_toast = None

        # Data for dialog
        self._last_hidden: list[str] = []  # pinned + kept-for-safety combined

        # Initial UI
        self._refresh_autoremove_label()
        self.pie_chart.queue_draw()

    # ─────────────── unused runtimes card ───────────────

    def _refresh_autoremove_label(self):
        if self.timeout_source:
            GLib.source_remove(self.timeout_source)
            self.timeout_source = None

        removable, pinned, kept = list_flatpak_unused_with_diag(self)
        # Cache for dialog — combine pinned + kept into a single list
        combined = []
        seen = set()
        for lst in (pinned, kept):
            for r in lst:
                if r not in seen:
                    seen.add(r); combined.append(r)
        self._last_hidden = combined

        # Show ONLY the Removable list in the label
        if not removable:
            self.pkg_list.set_text("Nothing unused to uninstall")
            self.remove_btn.set_sensitive(False)
        else:
            self.pkg_list.set_text("Removable: " + " ".join(removable))
            self.remove_btn.set_sensitive(True)

        # Enable the "What's hidden?" button if there is anything to show
        self.kept_btn.set_sensitive(bool(self._last_hidden))

        return GLib.SOURCE_REMOVE

    def _on_show_kept_clicked(self, _btn):
        # Build a simple dialog listing hidden items (pinned or safety-kept)
        dlg = Adw.Dialog.new()
        dlg.set_title("Hidden items")
        dlg.present(self)
        dlg.set_content_width(560)
        dlg.set_content_height(420)

        header = Adw.HeaderBar()
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        v.set_margin_top(12); v.set_margin_bottom(12); v.set_margin_start(12); v.set_margin_end(12)

        intro = Gtk.Label(
            label=("These items are hidden from removal because they’re either pinned by Flatpak "
                   "or kept for safety (e.g., base Platforms/Locales, SDKs, or essential extensions)."),
            xalign=0
        )
        intro.set_wrap(True)
        v.append(intro)

        sc = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        listbox = Gtk.ListBox(); listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        sc.set_child(listbox); v.append(sc)

        title = Gtk.Label(label="Hidden (pinned or safety-kept)", xalign=0)
        title.add_css_class("title-4")
        listbox.append(title)

        if not self._last_hidden:
            listbox.append(Gtk.Label(label="— none —", xalign=0))
        else:
            for ref in self._last_hidden:
                listbox.append(Adw.ActionRow(title=ref))

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda *_: dlg.close())
        v.append(close_btn)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.append(header); body.append(v)
        dlg.set_child(body)

    def _on_remove_clicked(self, _btn):
        run_flatpak_autoremove_async(self._refresh_autoremove_label)

    # ─────────────── clear temp / options ───────────────

    def _on_clear_clicked(self, _btn):
        if self._opts["sweep"]:
            self._current_toast = self._toast("Scanning cache directories...")
            GLib.Thread.new("cache_scanner", self._scan_cache_in_thread)
        else:
            if self._perform_instant_clears():
                self._toast("Selected caches cleared")
                self.pie_chart.queue_draw()

    def _perform_instant_clears(self):
        def rm_rf(p: Path) -> bool:
            try:
                if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
                elif p.exists(): p.unlink(missing_ok=True)
                return True
            except Exception:
                return False

        removed = False
        c = xdg_cache()
        if self._opts["thumbs"]:   removed |= rm_rf(c / "thumbnails")
        if self._opts["webkit"] :  removed |= rm_rf(c / "WebKitGTK") or rm_rf(c / "webkitgtk")
        if self._opts["fontconf"]: removed |= rm_rf(c / "fontconfig")
        if self._opts["mesa"]   :  removed |= rm_rf(c / "mesa_shader_cache")
        return removed

    def _on_options_clicked(self, _btn):
        win = Adw.PreferencesWindow(transient_for=self, modal=True, title="Preferences")
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="What to clear when you press “Clear temp”")
        page.add(group)

        def add_switch(title, subtitle, key):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=self._opts[key])
            row.connect("notify::active", lambda r, *_: self._opts.__setitem__(key, r.get_active()))
            group.add(row)

        add_switch("Thumbnail cache", "~/.cache/thumbnails", "thumbs")
        add_switch("WebKitGTK caches", "~/.cache/WebKitGTK or ~/.cache/webkitgtk", "webkit")
        add_switch("Fontconfig cache", "~/.cache/fontconfig", "fontconf")
        add_switch("Mesa shader cache", "~/.cache/mesa_shader_cache", "mesa")

        g2 = Adw.PreferencesGroup(title="General")
        page.add(g2)
        row = Adw.SwitchRow(
            title="General cache sweep", subtitle="Pick large items in ~/.cache to remove", active=self._opts["sweep"]
        )
        row.connect("notify::active", lambda r, *_: self._opts.__setitem__("sweep", r.get_active()))
        g2.add(row)

        hb = Adw.HeaderBar()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(hb); box.append(page)
        win.set_content(box)
        win.present()

    # ─────────────── cache sweep (host + sandbox) ───────────────

    def _scan_cache_in_thread(self):
        """
        Build the sweep list with:
          • host ~/.cache/* (each first-level item)
          • host ~/.var/app/*/cache  (one entry per app cache)
          • sandbox XDG_CACHE/* (each first-level item)
        """
        entries: list[tuple[Path, int, bool, bool]] = []

        for apath, sz in _host_first_level_cache_entries():
            entries.append((Path(apath), sz, True, True))

        for apath, sz in _host_app_cache_entries():
            entries.append((Path(apath), sz, True, True))

        for p, sz in _sandbox_first_level_cache_entries():
            entries.append((p, sz, True, False))

        GLib.idle_add(self._show_sweep_dialog, entries)
        return None

    def _show_sweep_dialog(self, entries: list[tuple[Path, int, bool, bool]]):
        if self._current_toast:
            self._current_toast.close(); self._current_toast = None

        dlg = Adw.Dialog.new()
        dlg.set_title("Cache sweep")
        dlg.present(self)
        dlg.set_content_width(720)
        dlg.set_content_height(520)

        header = Adw.HeaderBar()
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        v.set_margin_top(12); v.set_margin_bottom(12); v.set_margin_start(12); v.set_margin_end(12)

        title = Gtk.Label(label="Select the cache files to remove:", xalign=0)
        title.add_css_class("title-4"); v.append(title)

        sc = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        listbox = Gtk.ListBox(); listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        sc.set_child(listbox); v.append(sc)

        toggles: List[Gtk.Switch] = []
        paths: List[Path] = []
        deletable: List[bool] = []
        on_host_flags: List[bool] = []

        for p, sz, can_delete, on_host in entries:
            loc = "host" if on_host else "sandbox"
            row = Adw.ActionRow(title=p.name, subtitle=f"{p} ({loc}) — {human_size(sz)}")
            sw = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=can_delete)
            row.add_suffix(sw)
            listbox.append(row)
            toggles.append(sw); paths.append(p); deletable.append(can_delete); on_host_flags.append(on_host)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        sel_all = Gtk.CheckButton(label="Select all")
        rm_btn = Gtk.Button(label="Remove selected", sensitive=False)
        actions.append(sel_all); actions.append(rm_btn); v.append(actions)

        def update_btn(*_a):
            rm_btn.set_sensitive(any(s.get_active() and s.get_sensitive() for s in toggles))
        for s in toggles:
            s.connect("notify::active", update_btn)

        def _set_all(active: bool):
            for s in toggles:
                if s.get_sensitive():
                    s.set_active(active)
            update_btn()
        sel_all.connect("toggled", lambda b: _set_all(b.get_active()))

        def do_rm(_b):
            removed = 0
            host_targets: list[Path] = []
            for sw, p, can_delete, on_host in zip(toggles, paths, deletable, on_host_flags):
                if not can_delete or not sw.get_active():
                    continue
                if on_host:
                    host_targets.append(p)
                else:
                    try:
                        if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
                        else: p.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        pass
            if host_targets:
                for t in host_targets:
                    if not _is_allowed_host_target(t):
                        continue  # safety: refuse out-of-scope deletions
                    # --- MODIFICATION START ---
                    try:
                        # Use shutil.rmtree for host deletion as an alternative to 'rm -rf'
                        if t.is_dir(): shutil.rmtree(t, ignore_errors=True)
                        else: t.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        pass

            if removed:
                self._toast(f"Removed {removed} item(s)")
                self.pie_chart.queue_draw()
            dlg.close()

        rm_btn.connect("clicked", do_rm)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.append(header); body.append(v)
        dlg.set_child(body)

    # ─────────────── pie chart ───────────────

    def _draw_chart(self, _area, cr, w: int, h: int, _data):
        if cairo is None:
            layout = PangoCairo.create_layout(cr)
            layout.set_text("Cairo not available; chart disabled")
            layout.set_font_description(Pango.FontDescription("Cantarell 14"))
            cr.set_source_rgba(1, 1, 1, 0.8)
            tw, th = layout.get_pixel_size()
            cr.move_to((w - tw)/2, (h - th)/2); PangoCairo.show_layout(cr, layout); return

        total, used, free = disk_usage_home(); frac_used = (used / total) if total else 0.0
        col_used, col_free, col_bg, col_text = "#2ea3d6", "#51d08a", "#3a3a3a", "#e6e6e6"

        def set_hex(hexcol: str, a=1.0):
            rgba = Gdk.RGBA(); rgba.parse(hexcol)
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, a)

        pad = 24; size = max(0, min(w, h) - pad*2); r = size/2; cx, cy = pad + r, pad + r
        set_hex(col_bg); cr.arc(cx, cy, r, 0, 2*math.pi); cr.fill()

        start = -math.pi/2; used_ang = frac_used * 2*math.pi
        set_hex(col_used); cr.move_to(cx, cy); cr.arc(cx, cy, r, start, start+used_ang); cr.close_path(); cr.fill()
        set_hex(col_free); cr.move_to(cx, cy); cr.arc(cx, cy, r, start+used_ang, start+2*math.pi); cr.close_path(); cr.fill()

        pct = int(round(frac_used * 100))
        layout = PangoCairo.create_layout(cr); layout.set_text(f"{pct}%")
        layout.set_font_description(Pango.FontDescription("Cantarell Bold 40"))
        tw, th = layout.get_pixel_size(); set_hex(col_text, 0.95)
        cr.move_to(cx - tw/2, cy - th/2); PangoCairo.show_layout(cr, layout)

        def rim_label(a_mid, txt, col):
            set_hex(col); sx = cx + math.cos(a_mid)*(r-6); sy = cy + math.sin(a_mid)*(r-6)
            ex = cx + math.cos(a_mid)*(r+14); ey = cy + math.sin(a_mid)*(r+14)
            cr.set_line_width(2.0); cr.move_to(sx, sy); cr.line_to(ex, ey); cr.stroke()
            layout = PangoCairo.create_layout(cr); layout.set_text(txt)
            layout.set_font_description(Pango.FontDescription("Cantarell 12")); tw, th = layout.get_pixel_size()
            tx = max(pad, min(w - pad - tw, ex + (8 if math.cos(a_mid) >= 0 else -tw - 8)))
            ty = max(pad, min(h - pad - th, ey - th/2))
            set_hex("#e8f3ff" if col == col_used else "#defcee")
            cr.move_to(tx, ty); PangoCairo.show_layout(cr, layout)

        used_mid = start + used_ang/2 if used_ang > 0 else start
        free_mid = start + used_ang + (2*math.pi - used_ang)/2
        rim_label(used_mid, f"Used — {human_size(used)}", col_used)
        rim_label(free_mid, f"Free — {human_size(free)}", col_free)

    # ─────────────── small helper ───────────────

    def _toast(self, text: str):
        dlg = Adw.AlertDialog.new("Spruce", text)
        dlg.add_response("ok", "OK"); dlg.set_default_response("ok"); dlg.present(self)
        return dlg


# ─────────────────────────── app ───────────────────────────

class SpruceApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        Adw.init()
    def do_activate(self):
        (self.props.active_window or SpruceWindow(application=self)).present()


def main() -> int:
    return SpruceApp().run([])


if __name__ == "__main__":
    raise SystemExit(main())

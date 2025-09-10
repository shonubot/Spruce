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

# Optional cairo import (some distros split the Python binding)
try:
    import cairo  # type: ignore
except Exception:
    cairo = None  # chart will show a text fallback

APP_ID = "io.github.shonubot.Spruce"
IS_FLATPAK = os.environ.get("FLATPAK_ID") == APP_ID
SPRUCE_DEBUG = os.environ.get("SPRUCE_DEBUG") == "1"


# ─────────────────────────── helpers ─────────────────────────

def _find_ui() -> str:
    """Locate ui/window.ui both in dev trees and in Flatpak installs."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "ui" / "window.ui",               # repo: src/spruce -> ../../ui/window.ui
        Path.cwd() / "ui" / "window.ui",                       # running from repo root
        Path("/app/share/io.github.shonubot.Spruce/ui/window.ui"),
        Path("/app/share/spruce/ui/window.ui"),
        Path("/app/ui/window.ui"),                             # your current install path
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return str(Path("ui") / "window.ui")


def human_size(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024.0
    return f"{f:.1f}EiB"


def _gio_fs_usage(path: Path) -> Tuple[int, int, int] | None:
    """Read filesystem totals (size, free) via GIO. Returns (total, used, free)."""
    try:
        gfile = Gio.File.new_for_path(str(path))
        info = gfile.query_filesystem_info("filesystem::size,filesystem::free", None)
        total = int(info.get_attribute_uint64("filesystem::size"))
        free = int(info.get_attribute_uint64("filesystem::free"))
        used = max(0, total - free)
        if total > 0 and free >= 0 and used >= 0:
            return total, used, free
    except Exception:
        pass
    return None


def _host_view(path: Path) -> Path:
    """Map a path to host (/run/host/…) when sandboxed so disk stats are correct."""
    if not IS_FLATPAK:
        return path
    try:
        rel = path.resolve().relative_to("/")
        host = Path("/run/host") / rel
        if host.exists():
            return host
    except Exception:
        pass
    return path


def disk_usage_home() -> Tuple[int, int, int]:
    """Disk stats with fallbacks: host home → host root → sandbox home."""
    candidates: list[Path] = []
    if IS_FLATPAK:
        candidates += [_host_view(Path.home()), Path("/run/host")]
    candidates.append(Path.home())

    for p in candidates:
        ans = _gio_fs_usage(p)
        if ans:
            return ans
        try:
            total, used, free = shutil.disk_usage(str(p))
            total, used, free = int(total), int(used), int(free)
            if total > 0:
                return total, used, free
        except Exception:
            continue
    return 1, 0, 1


def xdg_cache() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))


def _host_flatpak_cmd(*args: str) -> list[str]:
    """Use host flatpak when sandboxed; otherwise normal flatpak."""
    return (["flatpak-spawn", "--host", "flatpak", *args]
            if IS_FLATPAK else ["flatpak", *args])


def _spawn_capture(cmd: list[str]) -> tuple[int, str, str]:
    """Run a command; return (exit_code, stdout, stderr)."""
    try:
        ok, out, err, status = GLib.spawn_sync(
            None, cmd, None,
            GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_PIPE | GLib.SpawnFlags.STDERR_TO_PIPE,
            None
        )
        code = int(status) if isinstance(status, int) else 0
        return code, (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace")
    except Exception as e:
        return 127, "", str(e)


def _debug_note(s: str):
    if not SPRUCE_DEBUG:
        return
    try:
        app = Gtk.Application.get_default()
        if app and app.props.active_window and hasattr(app.props.active_window, "pkg_list"):
            lbl = app.props.active_window.pkg_list
            old = lbl.get_text() or ""
            lbl.set_text((old + "\n" if old else "") + s)
    except Exception:
        pass


# ────────── Flatpak: detect unused runtimes/extensions (old/new friendly) ──────────

# Regexes for parsing
_RUNTIME_LINE = re.compile(r"^Runtime:\s*(.+?)\s*$", re.IGNORECASE)
_APP_ID_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_.-]+)+$")  # reverse-DNS ID

def _list_apps(scope_flag: str) -> list[str]:
    """Return app IDs installed in a given scope."""
    cmd = _host_flatpak_cmd("list", "--app", scope_flag)
    code, out, err = _spawn_capture(cmd)
    text = out if code == 0 else err
    apps: list[str] = []
    for ln in text.splitlines():
        tok = ln.strip().split()
        if tok and _APP_ID_TOKEN.match(tok[0]):
            apps.append(tok[0])
    if SPRUCE_DEBUG:
        _debug_note(f"{scope_flag} apps: {apps}")
    return apps


def _runtime_of_app(app_id: str, scope_flag: str) -> str | None:
    """Return runtime ref (prefixed with 'runtime/') for an app."""
    cmd = _host_flatpak_cmd("info", app_id, scope_flag)
    code, out, err = _spawn_capture(cmd)
    text = out if code == 0 else err
    for ln in text.splitlines():
        m = _RUNTIME_LINE.match(ln)
        if m:
            ref = m.group(1).strip()
            if not ref.startswith("runtime/"):
                ref = f"runtime/{ref}"
            return ref
    return None


def _used_runtime_refs(scope_flag: str) -> set[str]:
    """Set of runtime refs actually referenced by installed apps in this scope."""
    used: set[str] = set()
    for app in _list_apps(scope_flag):
        r = _runtime_of_app(app, scope_flag)
        if r:
            used.add(r)
    if SPRUCE_DEBUG:
        _debug_note(f"{scope_flag} used runtimes: {sorted(used)}")
    return used


def _list_runtime_ids(scope_flag: str) -> list[str]:
    """IDs of runtimes/extensions installed in this scope (old/new flatpak friendly)."""
    cmd = _host_flatpak_cmd("list", "--runtime", scope_flag)
    code, out, err = _spawn_capture(cmd)
    text = out if code == 0 else err
    ids: list[str] = []
    for ln in text.splitlines():
        tok = ln.strip().split()
        if tok and _APP_ID_TOKEN.match(tok[0]):  # same pattern
            ids.append(tok[0])
    if SPRUCE_DEBUG:
        _debug_note(f"{scope_flag} runtime IDs: {ids}")
    return ids


def _ref_of_id(rtid: str, scope_flag: str) -> str | None:
    """
    Turn a runtime/extension ID into a full ref by querying `flatpak info`.
    Prefer 'Ref:' line; fallback to composing from ID/Arch/Branch/Type.
    """
    cmd = _host_flatpak_cmd("info", rtid, scope_flag)
    code, out, err = _spawn_capture(cmd)
    text = out if code == 0 else err

    for ln in text.splitlines():
        if ln.lower().startswith("ref:"):
            ref = ln.split(":", 1)[1].strip()
            if not ref.startswith("runtime/") and not ref.startswith("app/"):
                ref = f"runtime/{ref}"
            return ref

    id_val = arch = branch = typ = ""
    for ln in text.splitlines():
        low = ln.lower()
        if low.startswith("id:"):
            id_val = ln.split(":", 1)[1].strip()
        elif low.startswith("arch:"):
            arch = ln.split(":", 1)[1].strip()
        elif low.startswith("branch:"):
            branch = ln.split(":", 1)[1].strip()
        elif low.startswith("type:"):
            typ = ln.split(":", 1)[1].strip().lower()
    if id_val and arch and branch:
        prefix = "runtime" if typ in ("", "runtime", None) else typ
        return f"{prefix}/{id_val}/{arch}/{branch}"
    return None


def _list_installed_runtime_refs(scope_flag: str) -> list[str]:
    """Full refs for installed runtimes/extensions in this scope."""
    refs: list[str] = []
    for rtid in _list_runtime_ids(scope_flag):
        ref = _ref_of_id(rtid, scope_flag)
        if ref and ref.startswith("runtime/"):
            refs.append(ref)
    if SPRUCE_DEBUG:
        _debug_note(f"{scope_flag} installed runtime refs: {refs}")
    return refs


def _base_of_runtime_ref(ref: str) -> tuple[str, str, str]:
    # runtime/org.freedesktop.Platform/x86_64/24.08 -> (org.freedesktop.Platform, x86_64, 24.08)
    parts = ref.split("/", 4)
    if len(parts) >= 4:
        return parts[1], parts[2], parts[3]
    return ref, "", ""


def _is_base_runtime(ref: str) -> bool:
    """
    Base runtimes look like runtime/org.gnome.Platform/arch/branch.
    Treat *.Locale and *.Debug as extensions.
    """
    name, _arch, _branch = _base_of_runtime_ref(ref)
    if name.endswith(".Locale") or name.endswith(".Debug"):
        return False
    return name.count(".") >= 2 and not any(seg in name for seg in ("Locale", "Debug"))


def _platform_base_from_extension(ref: str) -> str:
    """
    Map runtime/org.freedesktop.Platform.ffmpeg-full/x86_64/24.08 -> org.freedesktop.Platform
    Heuristic: first three dotted components.
    """
    name, _arch, _branch = _base_of_runtime_ref(ref)
    parts = name.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:3])
    return name


def _unused_refs_for_scope(scope_flag: str) -> list[str]:
    """
    Flatpak-like logic:
      - If there are **no apps installed** in this scope → consider **all installed
        runtime/extension refs** unused (pins/permissions are enforced at uninstall time).
      - Else → consider a ref unused if its base platform isn't among the runtimes
        actually referenced by any installed app.
    """
    installed = _list_installed_runtime_refs(scope_flag)
    apps = _list_apps(scope_flag)

    if not apps:
        # Match user expectation of `flatpak uninstall --unused` on old versions
        if SPRUCE_DEBUG:
            _debug_note(f"{scope_flag} has no apps; ALL runtimes/extensions are candidates")
        return installed[:]  # everything is a candidate (host will skip pinned)

    used_bases = {_base_of_runtime_ref(r)[0] for r in _used_runtime_refs(scope_flag)}

    unused: list[str] = []
    for ref in installed:
        name, _arch, _branch = _base_of_runtime_ref(ref)
        if _is_base_runtime(ref):
            if name not in used_bases:
                unused.append(ref)
        else:
            plat = _platform_base_from_extension(ref)
            if plat not in used_bases:
                unused.append(ref)

    # de-dup preserve order
    seen, out = set(), []
    for r in unused:
        if r not in seen:
            seen.add(r)
            out.append(r)
    if SPRUCE_DEBUG:
        _debug_note(f"{scope_flag} unused refs: {out}")
    return out


def list_flatpak_unused() -> list[str]:
    """Unused runtimes/extensions across user + system installations."""
    refs: list[str] = []
    for scope in ("--user", "--system"):
        refs.extend(_unused_refs_for_scope(scope))
    seen, out = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    if SPRUCE_DEBUG:
        _debug_note(f"unused refs (merged): {out}")
    return out


# ─────────────────── removal (user first, then system) ───────────────────

def run_flatpak_autoremove_async(on_done) -> None:
    """Removal via host flatpak. User scope first; system scope next (polkit may prompt)."""
    user_cmd = _host_flatpak_cmd("uninstall", "--unused", "--user", "-y")
    sys_cmd  = _host_flatpak_cmd("uninstall", "--unused", "--system", "-y")

    def _spawn(cmd):
        try:
            pid, _, _ = GLib.spawn_async(cmd, flags=GLib.SpawnFlags.SEARCH_PATH)
            return pid
        except Exception:
            return None

    pid1 = _spawn(user_cmd)

    def after_user(_pid, _cond):
        pid2 = _spawn(sys_cmd)
        if pid2:
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid2, lambda *_: GLib.timeout_add_seconds(1, on_done))
        else:
            GLib.timeout_add_seconds(1, on_done)

    if pid1:
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid1, after_user)
    else:
        after_user(None, None)


# ─────────────────────── file utilities ───────────────────────

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


# ─────────────────────── main window class ───────────────────────

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

        # Options state
        self._opts = {
            "thumbs": True,
            "webkit": True,
            "fontconf": True,
            "mesa": True,
            "sweep": True,  # enabled by default
        }

        self._current_toast = None

        # Initial UI
        self._refresh_autoremove_label()
        self.pie_chart.queue_draw()

    # ─────────────── unified cleanup card ───────────────

    def _refresh_autoremove_label(self):
        if self.timeout_source:
            GLib.source_remove(self.timeout_source)
            self.timeout_source = None

        pkgs = list_flatpak_unused()
        if pkgs:
            self.pkg_list.set_text(" ".join(pkgs))
            self.remove_btn.set_sensitive(True)
        else:
            self.pkg_list.set_text("No unused runtimes or extensions to remove.")
            self.remove_btn.set_sensitive(False)

        return GLib.SOURCE_REMOVE

    def _on_remove_clicked(self, _btn):
        run_flatpak_autoremove_async(self._refresh_autoremove_label)

    # ─────────────── clear temp / options ───────────────

    def _on_clear_clicked(self, _btn):
        if self._opts["sweep"]:
            self._current_toast = self._toast("Scanning cache directories...")
            GLib.Thread.new("cache_scanner", self._scan_cache_in_thread)
        else:
            removed = self._perform_instant_clears()
            if removed:
                self._toast("Selected caches cleared")
                self.pie_chart.queue_draw()

    def _perform_instant_clears(self):
        def rm_rf(p: Path) -> bool:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink(missing_ok=True)
                return True
            except Exception:
                return False

        removed = False
        cache = xdg_cache()
        if self._opts["thumbs"]:
            removed |= rm_rf(cache / "thumbnails")
        if self._opts["webkit"]:
            for name in ("WebKitGTK", "webkitgtk"):
                removed |= rm_rf(cache / name)
        if self._opts["fontconf"]:
            removed |= rm_rf(cache / "fontconfig")
        if self._opts["mesa"]:
            removed |= rm_rf(cache / "mesa_shader_cache")
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
            title="General cache sweep",
            subtitle="Pick large items in ~/.cache to remove",
            active=self._opts["sweep"],
        )
        row.connect("notify::active", lambda r, *_: self._opts.__setitem__("sweep", r.get_active()))
        g2.add(row)

        hb = Adw.HeaderBar()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(hb)
        box.append(page)
        win.set_content(box)
        win.present()

    # ─────────────── cache sweep dialog ───────────────

    def _cache_roots(self) -> list[tuple[Path, bool]]:
        roots: list[tuple[Path, bool]] = []
        unique_paths = set()

        main_cache = xdg_cache()
        if main_cache.is_dir():
            unique_paths.add(main_cache)

        if IS_FLATPAK:
            host_cache = _host_view(Path.home()) / ".cache"
            if host_cache.is_dir():
                unique_paths.add(host_cache)

            flatpak_app_dir = Path.home() / ".var" / "app"
            if flatpak_app_dir.is_dir():
                for app_dir in flatpak_app_dir.iterdir():
                    app_cache_dir = app_dir / "cache"
                    if app_cache_dir.is_dir():
                        unique_paths.add(app_cache_dir)

        for p in unique_paths:
            can_write = os.access(p, os.W_OK | os.X_OK)
            roots.append((p, bool(can_write)))
        return roots

    def _scan_cache_in_thread(self):
        entries: list[tuple[Path, int, bool]] = []
        for root, can_delete in self._cache_roots():
            try:
                for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                    try:
                        size = dir_size(child)
                        entries.append((child, size, can_delete))
                    except Exception:
                        pass
            except Exception:
                pass

        GLib.idle_add(self._show_sweep_dialog, entries)
        return None

    def _show_sweep_dialog(self, entries):
        if self._current_toast:
            self._current_toast.close()
            self._current_toast = None

        dlg = Adw.AlertDialog.new("Cache sweep", "Select the cache files to remove:")
        dlg.set_default_response("cancel")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Remove selected")

        # Build content
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        v.set_margin_top(12)
        v.set_margin_bottom(12)
        v.set_margin_start(12)
        v.set_margin_end(12)

        sc = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        sc.set_child(listbox)
        v.append(sc)

        toggles: List[Gtk.Switch] = []
        paths: List[Path] = []
        deletable: List[bool] = []

        for p, sz, can_delete in sorted(entries, key=lambda t: t[1], reverse=True)[:120]:
            row = Adw.ActionRow(title=p.name, subtitle=f"{p} — {human_size(sz)}")
            sw = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=can_delete)
            row.add_suffix(sw)
            listbox.append(row)
            toggles.append(sw)
            paths.append(p)
            deletable.append(can_delete)

        dlg.set_extra_child(v)

        def on_response(_d, resp):
            if resp != "remove":
                return
            removed = 0
            for sw, p, can_delete in zip(toggles, paths, deletable):
                if not can_delete:
                    continue
                if sw.get_active() and p.exists():
                    try:
                        if p.is_dir():
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            p.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        pass
            if removed:
                self._toast(f"Removed {removed} item(s)")
                self.pie_chart.queue_draw()

        dlg.connect("response", on_response)
        dlg.present(self)

    # ─────────────── pie chart ───────────────

    def _draw_chart(self, _area, cr, w: int, h: int, _data):
        if cairo is None:
            layout = PangoCairo.create_layout(cr)
            layout.set_text("Cairo not available; chart disabled")
            desc = Pango.FontDescription("Cantarell 14")
            layout.set_font_description(desc)
            cr.set_source_rgba(1, 1, 1, 0.8)
            tw, th = layout.get_pixel_size()
            cr.move_to((w - tw) / 2, (h - th) / 2)
            PangoCairo.show_layout(cr, layout)
            return

        total, used, free = disk_usage_home()
        frac_used = (used / total) if total else 0.0

        col_used = "#2ea3d6"
        col_free = "#51d08a"
        col_bg = "#3a3a3a"
        col_text = "#e6e6e6"

        def set_hex(hexcol: str, alpha=1.0):
            rgba = Gdk.RGBA()
            rgba.parse(hexcol)
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, alpha)

        pad = 24
        size = max(0, min(w, h) - pad * 2)
        r = size / 2
        cx, cy = pad + r, pad + r

        set_hex(col_bg)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()

        start = -math.pi / 2
        used_ang = frac_used * 2 * math.pi
        set_hex(col_used)
        cr.move_to(cx, cy)
        cr.arc(cx, cy, r, start, start + used_ang)
        cr.close_path()
        cr.fill()

        set_hex(col_free)
        cr.move_to(cx, cy)
        cr.arc(cx, cy, r, start + used_ang, start + 2 * math.pi)
        cr.close_path()
        cr.fill()

        pct = int(round(frac_used * 100))
        layout = PangoCairo.create_layout(cr)
        layout.set_text(f"{pct}%")
        layout.set_font_description(Pango.FontDescription("Cantarell Bold 40"))
        tw, th = layout.get_pixel_size()
        set_hex(col_text, 0.95)
        cr.move_to(cx - tw / 2, cy - th / 2)
        PangoCairo.show_layout(cr, layout)

        def rim_label(angle_mid, text, color_hex):
            set_hex(color_hex)
            sx = cx + math.cos(angle_mid) * (r - 6)
            sy = cy + math.sin(angle_mid) * (r - 6)
            ex = cx + math.cos(angle_mid) * (r + 14)
            ey = cy + math.sin(angle_mid) * (r + 14)
            cr.set_line_width(2.0)
            cr.move_to(sx, sy)
            cr.line_to(ex, ey)
            cr.stroke()

            layout = PangoCairo.create_layout(cr)
            layout.set_text(text)
            layout.set_font_description(Pango.FontDescription("Cantarell 12"))
            tw, th = layout.get_pixel_size()
            tx = max(pad, min(w - pad - tw, ex + (8 if math.cos(angle_mid) >= 0 else -tw - 8)))
            ty = max(pad, min(h - pad - th, ey - th / 2))
            set_hex("#e8f3ff" if color_hex == col_used else "#defcee")
            cr.move_to(tx, ty)
            PangoCairo.show_layout(cr, layout)

        used_mid = start + used_ang / 2 if used_ang > 0 else start
        free_mid = start + used_ang + (2 * math.pi - used_ang) / 2
        rim_label(used_mid, f"Used — {human_size(used)}", col_used)
        rim_label(free_mid, f"Free — {human_size(free)}", col_free)

    # ─────────────── small UI helpers ───────────────

    def _toast(self, text: str):
        dlg = Adw.AlertDialog.new("Spruce", text)
        dlg.add_response("ok", "OK")
        dlg.set_default_response("ok")
        dlg.present(self)
        return dlg

    def _info(self, text: str):
        self._toast(text)


# ─────────────────────────── app ─────────────────────────

class SpruceApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        Adw.init()

    def do_activate(self):
        win = self.props.active_window or SpruceWindow(application=self)
        win.present()


def main() -> int:
    app = SpruceApp()
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())

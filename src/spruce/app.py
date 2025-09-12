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
    """
    Run a command with Gio.Subprocess and capture stdout/stderr (UTF-8).
    Uses communicate_utf8() for portability across gi versions.
    """
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

def _host_sh(script: str) -> tuple[int, str, str]:
    return _run(_host_exec("bash", "-lc", script))


# ─────────────────── diagnostics shown in UI ───────────────────

def _append_diag(win: Gtk.Widget | None, lines: list[str]) -> None:
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
    # Try columns first (newer Flatpak)
    code, out, err = _run(_host_exec("flatpak", "list", "--app", scope, "--columns=application"))
    apps: list[str] = []
    if code == 0 and out.strip():
        for ln in out.splitlines():
            app = ln.strip()
            if APP_ID_RE.match(app):
                apps.append(app)

    # Fallback: parse default table (older Flatpak)
    if not apps:
        code2, out2, err2 = _run(_host_exec("flatpak", "list", "--app", scope))
        text = out2 if code2 == 0 else err2
        for ln in text.splitlines():
            toks = ln.split()
            for t in toks:
                if APP_ID_RE.match(t):
                    apps.append(t)
                    break
        if code != 0 and text.strip():
            _append_diag(None, [f"{scope} list(err): {text.strip()}"])

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


def _host_installed_runtime_refs(scope: str) -> list[str]:
    # Enumerate by filesystem; simple pipelines; no bash process-substitution
    if scope == "--user":
        script = r'''
set -e
roots=""
[ -n "$HOME" ] && [ -d "$HOME/.local/share/flatpak/runtime" ] && roots="$roots $HOME/.local/share/flatpak/runtime"
[ -n "$USER" ] && [ -d "/var/home/$USER/.local/share/flatpak/runtime" ] && roots="$roots /var/home/$USER/.local/share/flatpak/runtime"
[ -z "$roots" ] && exit 0
for r in $roots; do
  [ -d "$r" ] || continue
  find "$r" -mindepth 3 -maxdepth 3 -type d
done | sort \
| awk -F/ 'BEGIN{OFS="/"} {rid=$(NF-2); arch=$(NF-1); br=$NF; print "runtime/"rid"/"arch"/"br}' \
| awk "!x[$0]++"
'''
    else:
        script = r'''
set -e
r="/var/lib/flatpak/runtime"
[ -d "$r" ] || exit 0
find "$r" -mindepth 3 -maxdepth 3 -type d | sort \
| awk -F/ 'BEGIN{OFS="/"} {rid=$(NF-2); arch=$(NF-1); br=$NF; print "runtime/"rid"/"arch"/"br}' \
| awk "!x[$0]++"
'''
    code, out, err = _host_sh(script)
    if code != 0 and err.strip():
        _append_diag(None, [f"{scope} refs(err): {err.strip()}"])
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("runtime/")]


def _base_of(ref: str) -> tuple[str, str, str]:
    parts = ref.split("/", 4)
    return (parts[1], parts[2], parts[3]) if len(parts) >= 4 else (ref, "", "")


def _is_base_runtime(ref: str) -> bool:
    name, _, _ = _base_of(ref)
    if name.endswith(".Locale") or name.endswith(".Debug"):
        return False
    # Treat common extensions as non-base
    if any(name.endswith(s) for s in (".GL.default", ".ffmpeg-full", ".openh264", ".codecs", ".codecs-extra")):
        return False
    return True


def _platform_from_ext(ref: str) -> str:
    name, _, _ = _base_of(ref)
    parts = name.split(".")
    return ".".join(parts[:3]) if len(parts) >= 3 else name


# Extensions Flatpak tends to keep installed and which don't map cleanly to app runtimes.
_ALWAYS_KEEP_EXT_SUFFIXES = (
    ".GL.default",
    ".codecs",
    ".codecs-extra",
    ".openh264",
)

def _is_always_kept_extension(ref: str) -> bool:
    name, _, _ = _base_of(ref)
    return any(name.endswith(suf) for suf in _ALWAYS_KEEP_EXT_SUFFIXES)


def list_flatpak_unused_with_diag(win: Gtk.Widget) -> list[str]:
    diag: list[str] = []
    all_refs: list[str] = []

    for scope in ("--user", "--system"):
        refs = _host_installed_runtime_refs(scope)
        apps = _host_list_apps(scope)
        diag.append(f"{scope} installed: {len(refs)}")
        if SPRUCE_DEBUG:
            diag.append(f"{scope} refs: {refs}")
        diag.append(f"{scope} apps: {apps}")

        if not refs:
            continue

        if not apps:
            # If there are no apps at all in this scope, *everything* looks unused.
            # Still, skip always-kept extensions to avoid false positives.
            for ref in refs:
                if not _is_always_kept_extension(ref):
                    all_refs.append(ref)
            continue

        used_bases = set()
        for a in apps:
            r = _host_runtime_of_app(a, scope)
            if r:
                used_bases.add(_base_of(r)[0])
        if SPRUCE_DEBUG:
            diag.append(f"{scope} used bases: {sorted(used_bases)}")

        for ref in refs:
            if _is_always_kept_extension(ref):
                continue
            base = _base_of(ref)[0] if _is_base_runtime(ref) else _platform_from_ext(ref)
            if base not in used_bases:
                all_refs.append(ref)

    # de-dup
    seen, uniq = set(), []
    for r in all_refs:
        if r not in seen:
            seen.add(r); uniq.append(r)

    diag.append(f"unused refs (merged): {uniq}")
    _append_diag(win, diag)
    return uniq


def run_flatpak_autoremove_async(on_done) -> None:
    """
    Run `flatpak uninstall --unused` for user and system, count removals,
    and show a toast if nothing changed.
    """
    def _run_and_count(scope: str) -> int:
        code, out, err = _run(_host_exec("flatpak", "uninstall", "--unused", scope, "-y"))
        # Lines look like: "Uninstalling runtime/org.foo.Bar/x86_64/24.08"
        count = sum(1 for ln in (out or "").splitlines() if ln.strip().startswith("Uninstalling "))
        return count

    removed = 0
    try:
        removed += _run_and_count("--user")
    except Exception:
        pass
    try:
        removed += _run_and_count("--system")
    except Exception:
        pass

    def _after():
        # Refresh the UI
        on_done()
        # Also inform the user if nothing changed
        app = Gtk.Application.get_default()
        win = app.props.active_window if app else None
        try:
            if win and hasattr(win, "_toast"):
                if removed > 0:
                    win._toast(f"Removed {removed} unused item(s)")
                else:
                    win._toast("Flatpak reported nothing unused to uninstall")
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


def _host_cache_paths_and_sizes() -> list[tuple[str, int]]:
    # List host cache dirs and size them with du -sb (one call per path)
    code, out, err = _host_sh(r'''
set -e
paths=""
[ -d "$HOME/.cache" ] && paths="$paths $HOME/.cache"
if [ -d "$HOME/.var/app" ]; then
  while IFS= read -r d; do paths="$paths $d"; done <<EOF
$(find "$HOME/.var/app" -mindepth 2 -maxdepth 2 -type d -name cache -print)
EOF
fi
for p in $paths; do printf "%s\n" "$p"; done
''')
    if code != 0 and err.strip():
        _append_diag(None, [f"host cache list(err): {err.strip()}"])
        return []

    entries: list[tuple[str, int]] = []
    for p in [ln.strip() for ln in out.splitlines() if ln.strip().startswith("/")]:
        c2, o2, _e2 = _host_sh(f'du -sb "{p}" 2>/dev/null | awk \'{{print $1}}\'')
        try:
            sz = int(o2.strip() or "0")
        except Exception:
            sz = 0
        entries.append((p, sz))
    return entries


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

        # Options
        self._opts = {"thumbs": True, "webkit": True, "fontconf": True, "mesa": True, "sweep": True}
        self._current_toast = None

        # Initial UI
        self._refresh_autoremove_label()
        self.pie_chart.queue_draw()

    # ─────────────── unused runtimes card ───────────────

    def _refresh_autoremove_label(self):
        if self.timeout_source:
            GLib.source_remove(self.timeout_source)
            self.timeout_source = None

        pkgs = list_flatpak_unused_with_diag(self)
        if pkgs:
            self.pkg_list.set_text(" ".join(pkgs))
            self.remove_btn.set_sensitive(True)
        else:
            if not self.pkg_list.get_text():
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
        if self._opts["webkit"]:   removed |= rm_rf(c / "WebKitGTK") or rm_rf(c / "webkitgtk")
        if self._opts["fontconf"]: removed |= rm_rf(c / "fontconfig")
        if self._opts["mesa"]:     removed |= rm_rf(c / "mesa_shader_cache")
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
        entries: list[tuple[Path, int, bool, bool]] = []

        # sandbox cache roots
        sc = xdg_cache()
        if sc.is_dir():
            for child in sorted(sc.iterdir(), key=lambda p: p.name.lower()):
                try:
                    entries.append((child, dir_size(child), True, False))
                except Exception:
                    pass

        # host cache roots (absolute paths), with sizes
        for path, size in _host_cache_paths_and_sizes():
            entries.append((Path(path), size, True, True))

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

        for p, sz, can_delete, on_host in sorted(entries, key=lambda t: t[1], reverse=True):
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
            host_targets: list[str] = []
            for sw, p, can_delete, on_host in zip(toggles, paths, deletable, on_host_flags):
                if not can_delete or not sw.get_active():
                    continue
                if on_host:
                    host_targets.append(str(p))
                else:
                    try:
                        if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
                        else: p.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        pass
            if host_targets:
                for t in host_targets:
                    _run(_host_exec("bash", "-lc", f'rm -rf -- "{t}"'))
                removed += len(host_targets)

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

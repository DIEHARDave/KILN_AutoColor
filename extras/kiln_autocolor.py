# Map the slicer's tools to the IFS slots by color and material when a print
# starts, as zMod's auto color selection does ([kiln_autocolor])
#
# Kiln OS loads a plugin's extras/*.py while the plugin is enabled.
#
# Kiln's IFS sends slicer tool Tn to the slot its tool map names, slot n+1
# unless IFS_MAP_TOOL changed it for this print. This module wraps
# SDCARD_PRINT_FILE (what Moonraker, Mainsail, Fluidd and HelixScreen start
# prints with): before the print starts it reads the file's filament colors
# and types, picks the loaded slot that fits each tool the file uses, and
# sets the map with IFS_MAP_TOOL. Kiln resets the map when the print ends.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import itertools
import logging
import os
import re


LOGGER = logging.getLogger(__name__)

# Kiln's IFS: slicer tools T0-T3, slots 1-4 (IFS_MAP_TOOL's limits).
TOOLS = (0, 1, 2, 3)
SLOTS = (1, 2, 3, 4)
DEFAULT_MAP = {t: t + 1 for t in TOOLS}
# Slicers write the used filaments in the header and the colors and types in
# the config block at the end, so only both ends of the file are read.
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 512 * 1024
# Costs are color differences, CIE76 delta E in CIELAB (about 2: just
# visible, 100: black to white). RGB distance pairs colors the way the eye
# doesn't (red nearer olive than pink). A match this far off is "weak", and
# a tool or slot without a color counts as one.
WEAK_COST = 30.
# A slot of another material family is only used when nothing else fits.
MISMATCH_COST = 10 ** 6
# Defaults: an exact type over its family (PLA over PLA+) unless the family
# slot's color is this much closer; and two file colors keep slots of their
# own unless one of those would be this much further off than sharing (red
# stays red on a pink spool rather than turning orange like the orange tool).
FAMILY_COST = 10.
SHARE_COST = 60.
# Material families, longest names first where one contains another, so
# PLA+, PLA-CF and Silk PLA are all PLA and PETG is not PET.
FAMILIES = ("PETG", "PCTG", "PET", "PLA", "ABS", "ASA", "TPU", "TPE", "HIPS",
            "PVA", "BVOH", "PPS", "PP", "PC", "PA", "PE")
SAVE_VARIABLE = "autocolor_enabled"

COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
KEY_RE = re.compile(r"^;\s*([a-z_]+(?: [a-z_]+)*(?: \[[a-z0-9]+\])?)\s*=\s*(.*)$")
USED_RE = re.compile(r"^;\s*filament:\s*([0-9,\s]+)$")


def parse_color(text):
    """(r, g, b) from '#RRGGBB', 'RRGGBB' or '#RGB', else None."""
    m = COLOR_RE.match((text or "").strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def format_color(rgb):
    return "#%02X%02X%02X" % rgb if rgb else "-"


def _lab(rgb):
    """CIELAB (D65) of an sRGB color."""
    def lin(c):
        c /= 255.
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(float(c)) for c in rgb)
    xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047,
           (0.2126 * r + 0.7152 * g + 0.0722 * b),
           (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    f = [v ** (1. / 3.) if v > 216. / 24389. else (24389. / 27. * v + 16.)
         / 116. for v in xyz]
    return (116. * f[1] - 16., 500. * (f[0] - f[1]), 200. * (f[1] - f[2]))


def color_difference(a, b):
    """CIE76 delta E between two (r, g, b) colors."""
    return sum((x - y) ** 2 for x, y in zip(_lab(a), _lab(b))) ** 0.5


def family(material):
    """The base material ('PLA' for 'PLA+'), or the name itself."""
    name = (material or "").strip().upper()
    for base in FAMILIES:
        if re.search(r"(?<![A-Z])" + base, name):
            return base
    return name


def _split(value, sep):
    return [v.strip() for v in value.split(sep)] if value.strip() else []


def parse_gcode_info(text):
    """Filament info from G-code text (the head and tail of a file).

    Returns {'tools': [{'tool', 'color', 'type'}], 'source': str} for the
    tools the file uses."""
    keys = {}
    used = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(';'):
            continue
        m = USED_RE.match(line)
        if m and used is None:
            # OrcaSlicer / Bambu Studio header: used filaments, 1-based
            used = sorted({int(v) - 1 for v in _split(m.group(1), ',')
                           if v.isdigit() and int(v) > 0})
            continue
        m = KEY_RE.match(line.lower())
        if m and m.group(1) not in keys:
            keys[m.group(1)] = line.split('=', 1)[1].strip()
    colors = _split(keys.get('filament_colour', ''), ';')
    if not any(parse_color(c) for c in colors):
        colors = _split(keys.get('extruder_colour', ''), ';')
    types = _split(keys.get('filament_type', ''), ';')
    source = "header"
    zmod = keys.get('zmod_color_data')
    if zmod:
        # zmod_preprocess: "0,1,3|#rrggbb,...|PLA,..." (one entry per tool)
        parts = zmod.split('|')
        if len(parts) >= 3:
            idx = [int(v) for v in _split(parts[0], ',') if v.isdigit()]
            zc, zt = _split(parts[1], ','), _split(parts[2], ',')
            colors = [''] * (max(idx + [-1]) + 1)
            types = list(colors)
            for i, t in enumerate(idx):
                colors[t] = zc[i] if i < len(zc) else ''
                types[t] = zt[i] if i < len(zt) else ''
            used = idx
            source = "zmod_color_data"
    if used is None:
        lengths = _split(keys.get('filament used [mm]', '')
                         or keys.get('filament used [g]', ''), ',')
        try:
            if lengths:
                used = [i for i, v in enumerate(lengths) if float(v) > 0.]
                source = "filament used"
        except ValueError:
            used = None
    if used is None:
        used = list(range(max(len(colors), len(types))))
        source = "filament list"
    tools = []
    for t in used:
        color = parse_color(colors[t]) if t < len(colors) else None
        mtype = types[t].upper() if t < len(types) and types[t] else None
        tools.append({'tool': t, 'color': color, 'type': mtype})
    return {'tools': tools, 'source': source}


def read_gcode_info(path):
    """parse_gcode_info for a file, reading only its head and tail."""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        data = f.read(HEAD_BYTES)
        if size > HEAD_BYTES:
            f.seek(max(HEAD_BYTES, size - TAIL_BYTES))
            data += b'\n' + f.read()
    return parse_gcode_info(data.decode('utf-8', 'replace'))


def pair_cost(tool, slot, family_penalty):
    """(cost, note) of printing a tool from a slot."""
    cost, notes = 0., []
    ttype, stype = tool['type'], slot['type']
    if ttype and stype:
        if ttype == stype:
            pass
        elif family(ttype) == family(stype):
            cost += family_penalty
            notes.append("same family")
        else:
            cost += MISMATCH_COST
            notes.append("material mismatch")
    if tool['color'] is None or slot['color'] is None:
        cost += WEAK_COST
        notes.append("no color to match")
    else:
        d = color_difference(tool['color'], slot['color'])
        cost += d
        if tool['color'] == slot['color']:
            notes.append("exact")
        elif d >= WEAK_COST:
            notes.append("weak color match")
    return cost, notes


def plan_map(tools, slots, family_penalty=FAMILY_COST,
             share_penalty=SHARE_COST):
    """Best slot for each tool the file uses.

    tools: parse_gcode_info's list; slots: [{'slot', 'type', 'color'}] of
    the loaded slots only. Every combination is tried (at most 4^4): the sum
    of the pair costs, plus share_penalty for each tool sharing a slot with
    an earlier one, so two file colors only share a filament when giving one
    its own slot would be far off. Ties keep the default map.

    Returns {'map': {tool: slot}, 'rows': [...], 'problems': [...]}."""
    problems = []
    mappable = [t for t in tools if t['tool'] in TOOLS]
    for t in tools:
        if t['tool'] not in TOOLS:
            problems.append("T%d: Kiln's IFS has only T0-T3" % (t['tool'],))
    if not mappable:
        return {'map': {}, 'rows': [], 'problems': problems}
    if not slots:
        problems.append("no IFS slot has filament")
        return {'map': {}, 'rows': [], 'problems': problems}
    costs = {(t['tool'], s['slot']): pair_cost(t, s, family_penalty)
             for t in mappable for s in slots}
    best, best_cost = None, None
    for combo in itertools.product([s['slot'] for s in slots],
                                   repeat=len(mappable)):
        total = sum(costs[(t['tool'], s)][0] for t, s in zip(mappable, combo))
        total += share_penalty * (len(combo) - len(set(combo)))
        total += 0.001 * sum(1 for t, s in zip(mappable, combo)
                             if DEFAULT_MAP[t['tool']] != s)
        if best_cost is None or total < best_cost:
            best, best_cost = combo, total
    by_slot = {s['slot']: s for s in slots}
    result, rows = {}, []
    for t, s in zip(mappable, best):
        notes = list(costs[(t['tool'], s)][1])
        if list(best).count(s) > 1:
            notes.append("shares slot %d" % (s,))
        result[t['tool']] = s
        rows.append({'tool': t['tool'], 'slot': s, 'notes': notes,
                     'tool_color': format_color(t['color']),
                     'tool_type': t['type'] or "-",
                     'slot_color': format_color(by_slot[s]['color']),
                     'slot_type': by_slot[s]['type'] or "-"})
        if "material mismatch" in notes:
            problems.append("T%d %s: no loaded slot has %s filament" % (
                t['tool'], t['type'], family(t['type'])))
    return {'map': result, 'rows': rows, 'problems': problems}


def format_rows(rows):
    return ["T%d %s %s -> slot %d %s %s%s" % (
        r['tool'], r['tool_type'], r['tool_color'], r['slot'], r['slot_type'],
        r['slot_color'], " (%s)" % (", ".join(r['notes']),)
        if r['notes'] else "") for r in rows]


class KilnAutoColor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.enabled = config.getboolean('enabled', True)
        self.family_penalty = config.getfloat('family_penalty', FAMILY_COST,
                                              minval=0.)
        self.share_penalty = config.getfloat('share_penalty', SHARE_COST,
                                             minval=0.)
        self.stop_on_mismatch = config.getboolean('stop_on_mismatch', False)
        self.respect_manual_map = config.getboolean('respect_manual_map',
                                                    True)
        self.sdcard = None
        self.print_file = None
        self.last = {}
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.gcode.register_command("AUTOCOLOR", self.cmd_AUTOCOLOR,
                                    desc=self.cmd_AUTOCOLOR_help)
        self.gcode.register_command("AUTOCOLOR_ENABLE",
                                    self.cmd_AUTOCOLOR_ENABLE,
                                    desc=self.cmd_AUTOCOLOR_ENABLE_help)

    def _handle_connect(self):
        sv = self.printer.lookup_object('save_variables', None)
        if sv is not None:
            saved = sv.allVariables.get(SAVE_VARIABLE)
            if saved is not None:
                self.enabled = bool(saved)
        self.sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if self.sdcard is None:
            LOGGER.info("kiln_autocolor: no [virtual_sdcard], not wrapping "
                        "SDCARD_PRINT_FILE")
            return
        self.print_file = self.gcode.register_command("SDCARD_PRINT_FILE",
                                                      None)
        self.gcode.register_command(
            "SDCARD_PRINT_FILE", self.cmd_SDCARD_PRINT_FILE,
            desc="Start a print from a file (AutoColor maps its tools to "
                 "the IFS slots first)")

    # Printer state
    def _ifs(self):
        return self.printer.lookup_object('ifs', None)

    def _printing(self):
        sd = self.sdcard
        if sd is None:
            return False
        if hasattr(sd, 'is_active'):
            return sd.is_active()
        return getattr(sd, 'work_timer', None) is not None

    def _ifs_state(self):
        """(current map {tool: slot}, loaded slots [{'slot','type','color'}])
        or None when there is no IFS reading."""
        ifs = self._ifs()
        if ifs is None:
            return None
        now = self.printer.get_reactor().monotonic()
        st = ifs.get_status(now)
        if not st.get('connected') or st.get('loaded_channels') is None:
            return None
        mats = self.printer.lookup_object('ifs_materials', None)
        if mats is not None:
            slots_info = mats.get_status(now).get('slots', {})
        else:
            slots_info = ifs.materials_status().get('slots', {})
        tmap = {int(t): int(s) for t, s in st.get('tool_map', {}).items()}
        loaded = []
        for s in st['loaded_channels']:
            info = slots_info.get(str(s)) or {}
            mtype = (info.get('type') or "").upper() or None
            loaded.append({'slot': int(s), 'type': mtype,
                           'color': parse_color(info.get('color'))})
        return tmap, loaded

    def _path(self, filename):
        return os.path.join(self.sdcard.sdcard_dirname, filename.lstrip('/'))

    def _plan(self, filename):
        """(plan, current map) for a file; raises ValueError with a reason
        when there is nothing to plan."""
        state = self._ifs_state()
        if state is None:
            raise ValueError("no IFS reading")
        tmap, loaded = state
        info = read_gcode_info(self._path(filename))
        if not any(t['color'] or t['type'] for t in info['tools']):
            raise ValueError("no filament colors or types in the file")
        plan = plan_map(info['tools'], loaded, self.family_penalty,
                        self.share_penalty)
        plan['source'] = info['source']
        return plan, tmap

    def _apply(self, plan, tmap):
        changed = []
        for tool, slot in sorted(plan['map'].items()):
            if tmap.get(tool) != slot:
                self.gcode.run_script_from_command(
                    "IFS_MAP_TOOL TOOL=%d SLOT=%d" % (tool, slot))
                changed.append(tool)
        return changed

    def _remember(self, filename, plan, applied, note=""):
        self.last = {'filename': filename, 'applied': applied, 'note': note,
                     'map': {str(t): s for t, s in plan.get('map', {}).items()},
                     'rows': format_rows(plan.get('rows', [])),
                     'problems': list(plan.get('problems', []))}

    def _auto(self, gcmd, filename):
        if self._ifs() is None:
            return
        state = self._ifs_state()
        if state is None:
            gcmd.respond_info("AutoColor: no IFS reading, printing with the "
                              "current tool map")
            return
        tmap = state[0]
        if self.respect_manual_map and any(
                tmap.get(t, DEFAULT_MAP[t]) != DEFAULT_MAP[t] for t in TOOLS):
            gcmd.respond_info("AutoColor: tool map already set (%s), "
                              "leaving it" % (", ".join(
                                  "T%d->%d" % (t, tmap[t]) for t in TOOLS
                                  if t in tmap),))
            self._remember(filename, {}, False, "tool map already set")
            return
        try:
            plan, tmap = self._plan(filename)
        except (ValueError, OSError) as e:
            gcmd.respond_info("AutoColor: %s, printing with the current tool "
                              "map" % (e,))
            self._remember(filename, {}, False, str(e))
            return
        lines = ["AutoColor (%s):" % (plan['source'],)] \
            + format_rows(plan['rows']) \
            + ["!! %s" % (p,) for p in plan['problems']]
        if plan['problems'] and self.stop_on_mismatch:
            self._remember(filename, plan, False, "stopped")
            raise gcmd.error("\n".join(lines + [
                "AutoColor: print not started (stop_on_mismatch). Load the "
                "filament, or map by hand with IFS_MAP_TOOL and start "
                "again."]))
        self._apply(plan, tmap)
        self._remember(filename, plan, True)
        gcmd.respond_info("\n".join(lines))

    # Commands
    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if self.enabled and not self._printing():
            try:
                self._auto(gcmd, gcmd.get('FILENAME'))
            except self.printer.command_error:
                raise
            except Exception as e:
                LOGGER.exception("kiln_autocolor: auto mapping failed")
                gcmd.respond_info("AutoColor: failed (%s), printing with the "
                                  "current tool map" % (e,))
        self.print_file(gcmd)

    cmd_AUTOCOLOR_help = ("Show the slots AutoColor picks for a file: "
                          "FILENAME= (default: the last print), APPLY=1 sets "
                          "the tool map")
    def cmd_AUTOCOLOR(self, gcmd):
        if self.sdcard is None:
            raise gcmd.error("AUTOCOLOR: no [virtual_sdcard]")
        filename = gcmd.get('FILENAME', None)
        if not filename:
            ps = self.printer.lookup_object('print_stats', None)
            now = self.printer.get_reactor().monotonic()
            filename = ps.get_status(now).get('filename') if ps else None
        if not filename:
            raise gcmd.error("AUTOCOLOR: FILENAME= needed (no last print)")
        apply = gcmd.get_int('APPLY', 0)
        if apply and self._printing():
            raise gcmd.error("AUTOCOLOR: APPLY=1 only before a print")
        try:
            plan, tmap = self._plan(filename)
        except (ValueError, OSError) as e:
            raise gcmd.error("AUTOCOLOR: %s" % (e,))
        lines = ["AutoColor %s (%s, %s):" % (
            filename, plan['source'], "on" if self.enabled else "off")] \
            + format_rows(plan['rows']) \
            + ["!! %s" % (p,) for p in plan['problems']]
        if apply:
            self._apply(plan, tmap)
            self._remember(filename, plan, True, "AUTOCOLOR APPLY=1")
            lines.append("Tool map set for the next print")
        gcmd.respond_info("\n".join(lines))

    cmd_AUTOCOLOR_ENABLE_help = ("Map tools to IFS slots at print start: "
                                 "ENABLE=1|0 (kept in save_variables)")
    def cmd_AUTOCOLOR_ENABLE(self, gcmd):
        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        if enable is None:
            gcmd.respond_info("AutoColor is %s" % (
                "on" if self.enabled else "off",))
            return
        self.enabled = bool(enable)
        if self.printer.lookup_object('save_variables', None) is not None:
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (SAVE_VARIABLE,
                                                        enable))
        gcmd.respond_info("AutoColor %s" % ("on" if enable else "off",))

    def get_status(self, eventtime):
        return {'enabled': self.enabled, 'last': self.last}


def load_config(config):
    return KilnAutoColor(config)

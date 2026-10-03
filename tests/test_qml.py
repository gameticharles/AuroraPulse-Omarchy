"""Static checks for the QML layer.

These catch the failures that neither `qmllint` nor the Python tests see,
because they are runtime-only: a binding that reads a property of `undefined`,
an id that no longer exists, or a signal handler that overrides a base class's.

Each check below corresponds to a bug that was actually shipped and produced a
panel that loaded without a single error message while showing nothing.

Run with:  python3 tests/test_qml.py
"""

import os
import shutil
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QML = os.path.join(ROOT, "*.qml")
COMPONENTS = os.path.join(ROOT, "components", "*.qml")

sys.path.insert(0, os.path.join(ROOT, "src"))

# qmllint is available in this environment; skip rather than fail elsewhere.
HAVE_QMLLINT = subprocess.run(["sh", "-c", "command -v qmllint"],
                              capture_output=True).returncode == 0


def files():
    import glob
    out = sorted(glob.glob(QML) + glob.glob(COMPONENTS))
    return [path for path in out if os.path.basename(path) != "rowtest.qml"]


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def declared_ids(src):
    return set(re.findall(r"\bid:\s*([A-Za-z_]\w*)", src))


def declared_signals(src):
    return set(re.findall(r"^\s*signal\s+([A-Za-z_]\w*)\s*\(", src, re.M))


def declared_properties(src):
    props = set(re.findall(
        r"^\s*(?:required\s+|readonly\s+)?property\s+\w+\s+([A-Za-z_]\w*)",
        src, re.M))
    return props


class NoDeadWiring(unittest.TestCase):
    """QML fails quietly: the two bugs here both produced a panel that loaded
    without one error message while doing nothing.

    A signal that is declared and handled but never emitted, and a function
    that emits it but is itself never called, are the same bug seen from two
    ends. The genre chips were wired all the way to a request and then nothing
    ever asked for it, so the strip stayed empty and read as "no genres
    available" rather than as a defect.

    A handler is deliberately not accepted as evidence of anything: the panel
    connecting `onLoadGenres` is exactly what made this look wired while the
    emit was missing.
    """

    def test_every_declared_signal_is_emitted(self):
        checked = 0
        for path in files():
            body = read(path)
            # Strip the declarations first. `signal syncCatalogue(bool full)`
            # contains the name followed by a bracket, so searching the raw
            # text always matches the declaration and every dead signal slips
            # through - which is exactly what happened to syncCatalogue.
            emits = re.sub(r"^\s*signal\s+\w+[^\n]*$", "", body, flags=re.M)
            for name in sorted(set(re.findall(r"^\s*signal\s+(\w+)", body, re.M))):
                checked += 1
                # A dot is allowed in front: `root.signalName(...)` is the
                # normal way QML emits. Only a word character blocks the match,
                # which is what keeps `onSignalName` handlers from counting.
                emitted = re.search(r"(?<!\w)%s\s*\(" % re.escape(name), emits)
                self.assertIsNotNone(
                    emitted,
                    "%s declares signal %s but never emits it"
                    % (os.path.basename(path), name))
        self.assertGreater(checked, 0, "no signals were checked at all")

    def test_no_function_is_defined_and_never_used(self):
        """A function nothing calls is the emit that never happens.

        Counted as word occurrences across the whole QML tree: the definition
        is one, so anything that is never referenced again is dead. This is
        what catches a request function that is written, correct, and simply
        never wired to a lifecycle hook.
        """
        everything = "\n".join(read(path) for path in files())
        for path in files():
            body = read(path)
            for name in sorted(set(re.findall(r"^\s*function\s+(\w+)\s*\(", body, re.M))):
                # `onSomethingChanged` is a property-change handler, wired by
                # naming convention rather than called, so it is never
                # referenced by name. Written as a function rather than the
                # `onXChanged: {}` form, which is why it turns up here.
                if re.match(r"^on[A-Z]\w*Changed$", name):
                    continue
                # IpcHandler functions are the plugin's command line: they are
                # called by `omarchy-shell aurora-pulse <name>`, not from QML.
                if re.search(r"function\s+%s\(\)\s*:\s*\w+" % re.escape(name),
                             body):
                    continue
                # A plain word count, because calling through an id or the
                # root object is the normal way to call one: excluding a
                # leading dot flagged every `root.someFunction()` as unused.
                uses = len(re.findall(r"\b%s\b" % re.escape(name), everything))
                self.assertGreater(
                    uses, 1,
                    "%s defines function %s but nothing in the QML tree "
                    "references it" % (os.path.basename(path), name))


class NoUndefinedGuards(unittest.TestCase):
    """`x !== null` is TRUE for `undefined` in QML.

    The now-playing header used `item !== null`, which is true before the first
    state event arrives. The header then believed something was playing and read
    .title off undefined, so it rendered as empty space - with no error anywhere.
    """

    def test_no_bare_null_inequality_on_var_properties(self):
        """Only flag a var property that is actually *compared* to null.

        Declaring one is fine; `prop !== null` as the only guard is not,
        because that expression is true when the property is still undefined.
        """
        offenders = []
        for path in files():
            src = read(path)
            for name in declared_properties(src):
                # Any identifier compared against bare null, when the thing
                # being compared can still be undefined. Properties holding a
                # JSON value are the realistic case, so the guard has to hold
                # for every one of them, not just a `var` declaration.
                for match in re.finditer(
                        r"(?<![\w.])(\w+)\s*!==\s*null", src):
                    subject = match.group(1)
                    if subject in ("null", "undefined"):
                        continue
                    # A correct guard tests the same expression for undefined
                    # in the same breath, so look only at the enclosing
                    # statement - not at neighbouring lines, which is how a
                    # wide window hides the very bug this is looking for.
                    begin = src.rfind("\n", 0, match.start()) + 1
                    end = src.find("\n", match.end())
                    statement = src[begin:end if end != -1 else len(src)]
                    if "undefined" in statement or "typeof" in statement \
                            or "!!" in statement:
                        continue
                    line = src[:match.start()].count("\n") + 1
                    offenders.append("%s:%d %s !== null"
                                     % (os.path.basename(path), line, subject))
        self.assertEqual(offenders, [],
                         "compare against null only alongside undefined:\n"
                         + "\n".join(offenders))


class NoDanglingIds(unittest.TestCase):
    """An anchor pointing at an id that was renamed or deleted.

    The row delegate anchored to `artRight.left` after that id was removed
    during a theme change. Every row threw a ReferenceError, so the station list
    rendered as an empty box while the panel itself looked healthy.
    """

    GROUPS = {"anchors", "border", "parent", "grid", "flow", "font", "layer",
              "transform", "scale", "rotation", "opacity", "visible", "state",
              "children", "resources", "data", "Style", "Qt", "Model",
              "Quickshell", "Color", "Screen"}

    def test_anchor_targets_exist(self):
        bad = []
        for path in files():
            src = read(path)
            ids = declared_ids(src)
            used = set(re.findall(r"anchors\.\w+\s*:\s*([A-Za-z_]\w*)\.", src))
            for name in sorted(used):
                if name not in ids and name not in self.GROUPS:
                    bad.append("%s -> %s" % (os.path.basename(path), name))
        self.assertEqual(bad, [], "dangling anchor targets:\n" + "\n".join(bad))

    def test_handlers_are_present(self):
        """Every `onFoo:` handler must name a signal its component declares.

        Checked only for the components this plugin owns, one file at a time,
        so a nested Rectangle inside Panel.qml is never mistaken for a Browser.
        A handler for a signal that does not exist is a load error, and the
        shell reports it far from the cause.
        """
        lookup = {}
        for path in files():
            src = read(path)
            name = os.path.basename(path)[:-4]
            names = set(declared_signals(src))
            for prop in declared_properties(src):
                names.add(prop + "Changed")
            lookup[name] = names

        # A `onFooChanged:` handler for a property the *base class* declares is
        # just as valid as one for a local property, and this plugin's root
        # extends the shell's Panel. Without this, `onOpenedChanged` reads as a
        # handler for a signal that does not exist, which pushed the fix for
        # "the panel never re-reads the daemon" into being written as a
        # polling hack instead.
        for base, owner in (("Panel.qml", "Panel"),):
            shell = os.path.join("/usr/share/omarchy/shell/Ui", base)
            if owner in lookup and os.path.exists(shell):
                with open(shell, "r", encoding="utf-8") as handle:
                    for prop in declared_properties(handle.read()):
                        lookup[owner].add(prop + "Changed")

        # QML types and Qt's built-ins.
        builtin = {"clicked", "pressed", "released", "triggered", "moved",
                   "wheel", "wheelMoved", "activated", "entered", "exited",
                   "textChanged", "valueChanged", "checkedChanged",
                   "currentIndexChanged", "countChanged", "onTextChanged",
                   "onValueChanged", "onClicked", "onTriggered", "onMoved",
                   "onPressed", "onReleased", "onEntered", "onExited",
                   "onWheel", "onWheelMoved", "onActivated", "onVisibleChanged",
                   "onWidthChanged", "onHeightChanged", "onAccepted",
                   "onContainsMouseChanged", "onStatusChanged", "onRunning",
                   "onExited", "onActiveFocusChanged", "onAcceptedPressed",
                   "linksClicked", "onLinksClicked", "onLinkActivated",
                   "onScrolled", "onPositionChanged", "onMovementChanged",
                   "onOpenChanged", "onLayoutDirectionChanged"}

        bad = []
        for path in files():
            src = read(path)
            for match in re.finditer(
                    r"^\s*([A-Z][A-Za-z0-9_.]*)\s*\{", src, re.M):
                component = match.group(1).split(".")[-1]
                if component not in lookup:
                    continue
                start = match.end()
                depth, index = 1, start
                while index < len(src) and depth:
                    if src[index] == "{":
                        depth += 1
                    elif src[index] == "}":
                        depth -= 1
                    index += 1
                body = src[start:index]
                # Only the handlers bound directly to this component, not those
                # belonging to objects nested inside it.
                # A handler bound to the component itself sits at the very
                # start of a line with only the declaration's own indent in
                # front of it. Anything deeper belongs to a nested object.
                base = len(match.group(0)) - len(match.group(0).lstrip())
                for handler in re.findall(
                        r"^([ \t]*)on([A-Z][A-Za-z0-9_]*)[ \t]*:",
                        body, re.M):
                    if len(handler[0]) - len(handler[0].lstrip()) > base + 2:
                        continue        # belongs to a nested object
                    name = handler[1][0].lower() + handler[1][1:]
                    if name in lookup[component] or name in builtin:
                        continue
                    bad.append("%s: %s has no signal %s"
                               % (os.path.basename(path), component, name))
        self.assertEqual(bad, [], "handlers with no matching signal:\n"
                         + "\n".join(sorted(set(bad))))


class NoUndeclaredPropertyBindings(unittest.TestCase):
    """A parent binding a property its child does not declare.

    QML treats an unknown property on a declared type as a load error, so this
    is the check that turns a rename from the whole panel failing to appear
    into a test failure. Renaming a child property and updating the two places
    inside the child that read it is easy; updating the parent's binding at the
    same time is the part that gets forgotten, and nothing in the Python tests
    can see it because neither side is Python.

    The `country` duplication fixed alongside this was the same class of
    mistake in the other direction: two declarations of one property, which
    QML accepts and resolves silently to the last one.

    Component types only, one file at a time, so a `Rectangle` inside Panel.qml
    is never mistaken for a `Panel`. `onFoo:` is a signal handler rather than
    a property and is checked by NoDanglingIds instead; `id` is not a
    property at all. Panel is the Quickshell base type, so its own base
    properties are not in this tree and it is excluded.
    """

    # Values every QML object has, whatever its type: geometry, appearance and
    # the layout attached properties.
    INHERENT = {"width", "height", "x", "y", "z", "visible", "opacity", "color",
                "parent", "objectName", "enabled", "focus", "clip", "rotation",
                "scale", "transform", "border", "radius", "spacing", "padding",
                "state", "loops", "font", "fontColor", "anchors", "Layout",
                "implicitWidth", "implicitHeight", "active",
                # A Menu-based component (DownloadMenu) is a submenu by its title.
                "title"}

    # Properties a component gets from the Qt type at its root.
    FROM_BASE = {"ToolTip": {"text", "delay", "timeout"}}

    def components(self):
        import glob
        return {os.path.basename(path)[:-4]: read(path) for path in files()}

    def declared(self, src):
        names = set(declared_properties(src))
        # A declared signal is also bindable as a handler, and the root object
        # of a component exposes its own ids.
        names |= {name + "Changed" for name in declared_signals(src)}
        return names

    def block(self, src, start):
        depth, index = 1, start
        while index < len(src) and depth:
            if src[index] == "{":
                depth += 1
            elif src[index] == "}":
                depth -= 1
            index += 1
        return src[start:index]

    def test_every_bound_property_is_declared_by_its_component(self):
        known = self.components()
        bad = []
        checked = 0
        for path in files():
            src = read(path)
            for match in re.finditer(r"^\s*([A-Z][A-Za-z0-9_]*)\s*\{", src, re.M):
                name = match.group(1)
                if name not in known or name == "Panel":
                    continue
                base = len(match.group(0)) - len(match.group(0).lstrip())
                root_type = re.search(r"^([A-Z][\w.]*)\s*\{", known[name], re.M)
                allowed = (self.declared(known[name]) | self.INHERENT
                           | self.FROM_BASE.get(root_type.group(1) if root_type else "", set()))
                for bound in re.finditer(r"^([ \t]*)([a-z]\w*)\s*:",
                                         self.block(src, match.end()), re.M):
                    indent = len(bound.group(1)) - len(bound.group(1).lstrip())
                    if indent > base + 2:
                        continue        # belongs to an object nested inside
                    key = bound.group(2)
                    if key == "id" or key.startswith("on"):
                        continue
                    checked += 1
                    if key in allowed:
                        continue
                    line = src[:match.start() + bound.start()].count("\n") + 1
                    bad.append("%s:%d  <%s %s: ...>  but %s declares no \'%s\'"
                               % (os.path.basename(path), line, name, key,
                                  name, key))
        self.assertEqual(bad, [], "properties bound to a component that does "
                                  "not declare them:\n" + "\n".join(bad))
        self.assertGreater(checked, 50,
                           "only %d bindings were inspected, which is too few "
                           "for this check to mean anything" % checked)

    def test_no_property_is_declared_twice(self):
        """QML permits redeclaring a property and keeps the last one.

        Both declarations of `country` were "" so the panel worked, which is
        what made it survive: nothing looked wrong, and the duplicate only
        became visible by reading the file. A redeclaration is only ever an
        accident, so it is always a defect.

        A duplicate is only a duplicate within a single object scope: four
        separate delegates in Browser.qml each declare their own `modelData`,
        which is required of them and perfectly correct.
        """
        bad = []
        for path in files():
            src = read(path)
            for outer in re.finditer(r"^\s*([A-Z][A-Za-z0-9_.]*|[a-z]\w*)\s*\{",
                                     src, re.M):
                indent = len(outer.group(0)) - len(outer.group(0).lstrip())
                seen = {}
                depth, index = 1, outer.end()
                start = index
                while index < len(src) and depth:
                    if src[index] == "{":
                        depth += 1
                    elif src[index] == "}":
                        depth -= 1
                    index += 1
                body = src[start:index - 1 if depth == 0 else index]
                for match in re.finditer(
                        r"^([ \t]*)(?:required\s+|readonly\s+)?property"
                        r"\s+\w+\s+([A-Za-z_]\w*)", body, re.M):
                    own = len(match.group(1)) - len(match.group(1).lstrip())
                    if own > indent + 2:
                        continue    # belongs to an object nested deeper
                    seen.setdefault(match.group(2), []).append(
                        src[:start + match.start()].count("\n") + 1)
                for name, lines in sorted(seen.items()):
                    if len(lines) > 1:
                        bad.append("%s: %s declared at %s inside the same %s"
                                   % (os.path.basename(path), name, lines,
                                      outer.group(1)))
        self.assertEqual(bad, [], "duplicate property declarations:\n"
                          + "\n".join(bad))

    def test_a_declared_property_is_actually_used(self):
        """A property nothing reads and nothing writes is a collision waiting
        to happen.

        `property var channels` in Panel.qml was declared once and never used.
        The shell refused to instantiate the widget with "Duplicate property
        name", so there was no icon in the bar at all - while the player kept
        running from an earlier session and made the plugin look alive. It
        matched a name that exists in the Quickshell modules the panel imports,
        and neither qmllint nor the manifest validator mentions it.

        Only top-level properties of a component are checked. Delegate
        properties such as modelData are supplied by the view, and a readonly
        property computed from others is read from its own binding.
        """
        # Properties the shell reads off a widget by convention, using a
        # computed key, so no reference to them exists in our own QML:
        # Bar.qml looks up `openPanelIndicatorWidth` (or the height variant
        # when the bar is vertical) with `key in activeItem`. A static check
        # cannot see that, and flagging it would be a false positive about a
        # property that is genuinely load-bearing.
        SHELL_READS = {"openPanelIndicatorWidth", "openPanelIndicatorHeight"}

        known = self.components()
        bad = []
        for path in files():
            src = read(path)
            own = src
            for name in sorted(self.declared(src)):
                if name in SHELL_READS:
                    continue
                if name in ("modelData", "index"):
                    continue
                declared = re.search(
                    r"^  (?:required |readonly )?property \w+ %s\b" % name,
                    own, re.M)
                if not declared:
                    continue    # not top-level here
                # Counted across the whole tree because a panel property is
                # normally read by a child as `root.name`, so a dot in front
                # of the name is the ordinary case rather than a different one.
                # An internal assignment counts too: a property the component
                # sets on itself is used even if nothing outside reads it.
                everything = "\n".join(known.values())
                uses = len(re.findall(r"(?<![\w])%s\b" % re.escape(name),
                                      everything))
                # One is the declaration itself.
                if uses > 1:
                    continue
                line = src[:declared.start()].count("\n") + 1
                bad.append("%s:%d  %s  -- declared, never read"
                           % (os.path.basename(path), line, name))
        self.assertEqual(bad, [], "declared properties that nothing uses:\n"
                          + "\n".join(bad))

    def test_required_properties_are_actually_bound(self):
        """QML enforces this at load; a rename should fail the test instead.

        `required` exists precisely so that a missing binding cannot go
        unnoticed, and these eight were all introduced with the delegates
        that use them. Checking the obligation here means the check still runs
        when qmllint is unavailable, and names the file when it fails.
        """
        known = self.components()
        bad = []
        for path in files():
            src = read(path)
            # Required properties inside delegates are supplied by the model
            # rather than by a binding, so only top-level ones are ours to check.
            for match in re.finditer(
                    r"^\s*required\s+property\s+\w+\s+([A-Za-z_]\w*)",
                    src, re.M):
                name = match.group(1)
                if "modelData" in name or name == "index":
                    continue        # supplied by ListView's delegate model
                if not re.search(r"(?<!\.)\b%s\s*:" % re.escape(name), src):
                    bad.append("%s: required %s is never bound"
                               % (os.path.basename(path), name))
        self.assertEqual(bad, [], "required properties with no binding:\n"
                          + "\n".join(bad))


class NoSignalShadowing(unittest.TestCase):
    """A property `foo` auto-generates `fooChanged`.

    Declaring a signal with the same name is a hard error at load time.
    """

    def test_no_property_shadows_its_own_change_signal(self):
        bad = []
        for path in files():
            src = read(path)
            signals = declared_signals(src)
            for prop in declared_properties(src):
                if prop + "Changed" in signals:
                    bad.append("%s: %sChanged" % (os.path.basename(path), prop))
        self.assertEqual(bad, [], "signal/property collision:\n" + "\n".join(bad))


class PanelViewsAreMutuallyExclusive(unittest.TestCase):
    """The settings gear could be clicked forever and never show settings.

    The three alternative views (lyrics, guide, settings) share one StackLayout
    whose index is resolved by a priority chain, so lyrics and guide silently
    win whenever they are open. `openSettings` cleared neither of them, so
    after opening the TV guide the gear button set a flag that the index never
    looked at: the panel stayed on the guide with no error anywhere. Each
    toggle has to close the other two.
    """

    VIEWS = ("lyricsOpen", "guideOpen", "settingsOpen")

    def test_each_toggle_closes_the_other_two(self):
        body = read(os.path.join(ROOT, "Panel.qml"))
        for name in ("openLyrics", "openGuide", "openSettings"):
            match = re.search(
                r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n  \}" % name, body, re.S)
            self.assertIsNotNone(match, "%s is not defined" % name)
            text = match.group(1)
            for other in self.VIEWS:
                if other in (name[0].lower() + name[1:] + "Open",):
                    continue
                self.assertRegex(
                    text, r"\b%s\b" % other,
                    "%s does not mention %s, so the two views can both be "
                    "open at once and the StackLayout priority decides which "
                    "one the user actually sees" % (name, other))

    def test_the_stack_index_covers_every_view(self):
        """A view with no branch in the index chain is simply unreachable."""
        body = read(os.path.join(ROOT, "Panel.qml"))
        index = re.search(r"currentIndex:\s*(.+)", body)
        self.assertIsNotNone(index, "the StackLayout has no explicit index")
        for view in self.VIEWS:
            self.assertIn(view, index.group(1),
                          "%s is missing from the StackLayout index, so it "
                          "can never be shown" % view)


def balanced_block(src, marker, opening="before"):
    """The braces-balanced block that contains `marker`.

    Used to ask where an object actually sits in the tree rather than guessing
    from indentation. A `}` inside a string or comment would throw this off,
    but the only files it runs against are the small hand-written ones.

    `opening` says which side of the marker the opening brace is on. QML puts
    `Rectangle {` before its `id:` but `function foo() {` and `delegate: Foo {`
    after theirs, and picking the wrong one silently returns the wrong block -
    which is worse than not having the helper at all, because the test then
    passes for the wrong reason.
    """
    at = src.index(marker)
    if opening == "after":
        start = src.index("{", at)
    else:
        start = src.rindex("{", 0, at)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:]


class NoStrayOneLineObjects(unittest.TestCase):
    """`IconButton { a; b }` on one line.

    qmllint accepts it. The shell's parser does not, and the failure is
    reported as a syntax error in an unrelated part of the file.
    """

    def test_no_semicolon_separated_object_literals(self):
        """`IconButton { a; b }` on one line.

        qmllint accepts it; the shell's parser does not, and reports the
        failure as a syntax error somewhere unrelated. JS function bodies and
        signal handlers use braces and semicolons legitimately, so this only
        looks at lines that open an object declaration.
        """
        declaration = re.compile(r"^\s*([A-Z][A-Za-z0-9_.]*|[a-z][A-Za-z0-9_]*)\s*\{")
        keyword = re.compile(r"^\s*(function|on[A-Z]|else|if|for|while|return)\b")
        bad = []
        for path in files():
            for number, line in enumerate(read(path).splitlines(), 1):
                stripped = line.strip()
                if keyword.match(stripped):
                    continue
                if not declaration.match(line):
                    continue
                if not stripped.endswith("}"):
                    continue
                body = stripped[:-1]
                if "{" in body and ";" in body and "://" not in body:
                    bad.append("%s:%d %s" % (os.path.basename(path), number,
                                             stripped[:70]))
        self.assertEqual(bad, [], "one-line object literals:\n" + "\n".join(bad))


class TheCountryDropdownSitsBesideTheSearchField(unittest.TestCase):
    """Search and country are one row, built from the shell's own controls.

    The country filter used to be a hand-drawn menu of two hundred names with
    no way to type. It is the shell's searchable dropdown now - the same one
    the other Omarchy panels use - with the station count beside each country,
    sitting at the right of the search field it narrows together with.
    """

    def _src(self):
        return read(os.path.join(ROOT, "Browser.qml"))

    def test_the_dropdown_is_the_shells_searchable_one(self):
        src = self._src()
        picker = balanced_block(src, "id: countryPicker")
        self.assertIn("Ui.SearchableDropdown", src[:src.index("id: countryPicker")][-80:])
        self.assertIn('placeholderText: "Search countries"', picker)
        self.assertIn("root.countryOptions", picker)

    def test_it_sits_beside_the_search_field(self):
        src = self._src()
        field = src.index("id: searchField")
        picker = src.index("id: countryPicker")
        self.assertLess(field, picker)
        self.assertIn("countryPicker.left", balanced_block(src, "id: searchField"),
                      "the field must stop where the dropdown starts")

    def test_each_country_says_how_much_is_behind_it(self):
        src = self._src()
        block = src[src.index("readonly property var countryOptions"):][:600]
        self.assertIn("description:", block)
        self.assertIn('"All countries"', block)

    def test_the_two_visibilities_are_defined_once(self):
        src = self._src()
        self.assertIn("readonly property bool showCountry", src)
        self.assertIn("visible: root.showCountry", balanced_block(src, "id: countryPicker"))


class TransportButtonsSayWhenThereIsNowhereToGo(unittest.TestCase):
    """Two of the three transport buttons were always dead.

    `next` was gated on a guess the panel made from its own copy of the queue,
    and `previous` had no gate at all - it accepted the click and did nothing.
    The daemon owns the queue, so it is the one that says whether there is
    anywhere to step to (`hasNext`, `hasPrev`), and the buttons follow that.
    Previous also restarts the current item, but never on a live stream, where
    there is no beginning to go back to.
    """

    def _src(self):
        return read(os.path.join(ROOT, "Panel.qml"))

    def test_both_step_buttons_follow_the_daemon(self):
        src = self._src()
        self.assertIn("enabled: root.hasNext", src,
                      "next must follow the daemon's answer")
        self.assertIn("enabled: root.hasPrev", src,
                      "previous must follow the daemon's answer")
        self.assertIn("hasNext = !!msg.hasNext", src)
        self.assertIn("hasPrev = !!msg.hasPrev", src)

    def test_a_live_stream_offers_no_restart(self):
        from apctl.core import daemon as daemon_module
        events = []
        instance = daemon_module.Daemon.__new__(daemon_module.Daemon)
        instance.emit = events.append
        instance.queue = [{"uid": "radio:x", "source": "radio", "title": "X"}]
        instance.index = 0
        instance.repeat = "off"
        instance.shuffle = False
        instance._current = instance.queue[0]
        instance._props = {}
        instance._error = ""
        instance._loading = False
        instance._mute = False
        instance.player = None
        instance._session_video = False
        instance._on = lambda key, default=None: default
        instance.emit_state()
        state = events[-1]
        self.assertFalse(state["hasPrev"],
                         "a single live station has nowhere to go back to")
        self.assertFalse(state["hasNext"])

    def test_a_list_of_stations_can_be_stepped_through(self):
        from apctl.core import daemon as daemon_module
        events = []
        instance = daemon_module.Daemon.__new__(daemon_module.Daemon)
        instance.emit = events.append
        instance.queue = [{"uid": "radio:%d" % i, "source": "radio",
                           "title": str(i)} for i in range(3)]
        instance.index = 1
        instance.repeat = "off"
        instance.shuffle = False
        instance._current = instance.queue[1]
        instance._props = {}
        instance._error = ""
        instance._loading = False
        instance._mute = False
        instance.player = None
        instance._session_video = False
        instance._on = lambda key, default=None: default
        instance.emit_state()
        self.assertTrue(events[-1]["hasPrev"])
        self.assertTrue(events[-1]["hasNext"])


class RowButtonsAreNotBuriedUnderTheRow(unittest.TestCase):
    """The per-row buttons could not be clicked.

    The delegate declared a full-row MouseArea *after* the action buttons.
    Siblings in QML take input in reverse declaration order, so the row area
    sat on top of both buttons and swallowed every click on them. Playing still
    worked, because the row area plays too - which made the buttons look
    merely decorative rather than broken, and "add to queue" quietly did
    nothing however many times it was pressed.

    Declaration order is the fix, so this test pins the order rather than the
    geometry: the row MouseArea has to come first.
    """

    def _delegate(self):
        """The list-row delegate specifically.

        Not `delegate: Rectangle`, which matches the tab strip first and hands
        back a completely different object - a test that passes for the wrong
        reason is worse than no test.
        """
        src = read(os.path.join(ROOT, "Browser.qml"))
        return balanced_block(src, "id: row\n")

    def test_the_row_mouse_area_comes_before_the_buttons(self):
        delegate = self._delegate()
        row_area = delegate.index("id: rowArea")
        buttons = delegate.index("id: actions")
        self.assertLess(
            row_area, buttons,
            "the full-row MouseArea is declared after the action buttons, so it "
            "is drawn on top of them and every click on them plays the row "
            "instead of reaching the button")

    def test_the_row_is_still_clickable(self):
        delegate = self._delegate()
        block = balanced_block(delegate, "id: rowArea")
        self.assertIn("anchors.fill: parent", block,
                      "the whole row must stay clickable, not just its text")
        self.assertIn("root.play(row.modelData, row.index)", block,
                      "clicking a row must play that entry, with its position "
                      "so the list it came from becomes the queue")
        self.assertNotIn("onDoubleClicked", block,
                         "a double click also fires the first click's play")


class FacetRepliesAreNotTheStationList(unittest.TestCase):
    """A tab showing 250 country names where the stations should be.

    `handle()` branched on a reply's mode for the genre and country lists, and
    then fell through to `items = msg.items` for everything that was left. The
    facet lists are fetched the moment a tab becomes visible - which is *after*
    the browse that fills the list - so the station list was reliably replaced
    by the facet one: countries on the radio tab, categories on the television
    one. Nothing errored and no request failed. The rows rendered, they just
    were not stations, could not be played, and looked exactly like a tab whose
    download had quietly failed.

    A reply also has to be matched to the request it answers. The daemon works
    on a thread pool, so a slow browse can come back after the user has typed a
    search, or after they have moved to another tab - and the list under the
    cursor is then whichever reply happened to arrive last.
    """

    def _handle_list(self):
        src = read(os.path.join(ROOT, "Panel.qml"))
        return balanced_block(src, "function handleList(msg)", opening="after")

    def test_a_facet_reply_ends_in_its_own_branch(self):
        block = self._handle_list()
        facet = block.index("var facet = facetRequests[msg.id]")
        assign = block.index("var page = msg.items")
        self.assertLess(facet, assign,
                        "facet replies must be recognised before the list is "
                        "assigned")
        branch = balanced_block(block, "if (facet)", opening="after")
        self.assertIn("return", branch,
                      "a facet reply reaches `items = msg.items` and replaces "
                      "the stations with the facet list")

    def test_only_the_newest_reply_for_the_visible_tab_is_the_list(self):
        block = self._handle_list()
        guard = block.index("var page = msg.items")
        self.assertIn("msg.source !== source", block[:guard],
                      "a reply for a tab the user has left lands in the open one")
        self.assertIn("msg.id !== listRequestId", block[:guard],
                      "an older reply lands after the newer one and undoes it")

    def test_the_panel_knows_which_request_the_list_is_waiting_for(self):
        src = read(os.path.join(ROOT, "Panel.qml"))
        self.assertIn("property int listRequestId", src,
                      "the panel has to remember the id of the request it made")
        self.assertIn("return message.id", balanced_block(
            src, "function request(cmd, extra)", opening="after"),
            "a request has to hand its id back, or nothing can match the reply")
        self.assertIn("listRequestId = request(\"source\", listArgs())",
                      balanced_block(src, "function loadSource()", opening="after"),
                      "the list request is the one the panel is waiting for")
        self.assertIn("facetRequests", balanced_block(
            src, "function loadFacets()", opening="after"),
            "facet requests must be remembered by id so their replies are "
            "routed to the right tab")


class SwitchedTabsShowWhatTheyAlreadyHad(unittest.TestCase):
    """Every tab change emptied the panel and showed a spinner.

    `selectSource` set `items = []` and then asked the daemon for a list it had
    already been given. Television is eleven thousand channels and YouTube is a
    network round trip, so the panel sat empty and spinning every single time
    you moved between tabs - which reads as the tabs being slow or broken
    rather than as a list that was already in memory.

    The fix is a per-source snapshot restored before the refresh starts, so the
    refresh fills in rather than gates the view.
    """

    def test_panel_keeps_a_snapshot_per_source(self):
        src = read(os.path.join(ROOT, "Panel.qml"))
        self.assertIn("property var tabState", src,
                      "the panel must keep each tab's list and filters")
        block = balanced_block(src, "function selectSource(name, fromUser)",
                               opening="after")
        self.assertIn("saveTab()", block,
                      "leaving a tab must remember what it showed")
        self.assertIn("items = saved.items", block,
                      "returning to a tab must restore its list at once rather "
                      "than clearing it to an empty spinner")

    def test_a_snapshot_carries_its_own_filters(self):
        """A list restored under someone else's filters looks like it worked."""
        src = read(os.path.join(ROOT, "Panel.qml"))
        block = balanced_block(src, "function saveTab()", opening="after")
        for field in ("browseMode", "searchText", "country", "genre", "group",
                      "folder", "items"):
            self.assertIn(field, block,
                          "the snapshot must keep %s with the list it "
                          "produced" % field)

    def test_settings_do_not_drag_the_browser_back_to_its_default_tab(self):
        """The panel asks for settings every time it opens. Applying the
        default tab on each reply jumped the browser back to Radio while it
        was showing another tab's list."""
        src = read(os.path.join(ROOT, "Panel.qml"))
        case = src[src.index('case "settings":'):src.index('case "state":')]
        self.assertIn("!userPickedTab", case)

    def test_the_browser_says_when_it_is_refreshing_over_cached_rows(self):
        """Quiet is its own lie, just a smaller one than a permanent spinner."""
        src = read(os.path.join(ROOT, "Browser.qml"))
        self.assertIn(
            "root.loading && root.items.length > 0", src,
            "a refresh running over visible rows needs to say so")


class RowsSayWhichSourceTheyAre(unittest.TestCase):
    """A channel and a station looked identical in a row.

    Radio and TV share a browse, a search box and a row shape, so the only
    thing distinguishing a television channel from a radio station was the
    colour of the tab above the list - which shows the source being *looked at*,
    not where each row came from. Worse, `displayMeta` printed "live" for both,
    which for radio is true of every station ever and therefore says nothing
    while covering up the bitrate.

    The badge is deliberately scoped to where confusion is actually possible.
    Inside the YouTube or Music tab every row is from that source, so a badge
    on each one is pure width.
    """

    def _delegate(self):
        """The list-row delegate specifically.

        Not `delegate: Rectangle`, which matches the tab strip first and hands
        back a completely different object - a test that passes for the wrong
        reason is worse than no test.
        """
        src = read(os.path.join(ROOT, "Browser.qml"))
        return balanced_block(src, "id: row\n")

    def test_rows_do_not_repeat_the_tab_name(self):
        """Every row in a tab comes from that tab's source, so a per-row
        "Radio" badge repeated the tab name sixty times and took the width the
        station name needed. The tab says where the rows come from."""
        delegate = self._delegate()
        self.assertNotIn("id: sourceBadge", delegate)

    def test_radio_rows_do_not_all_say_live(self):
        src = read(os.path.join(ROOT, "Browser.qml"))
        block = balanced_block(src, "function displayMeta", opening="after")
        self.assertIn(
            "entry.source !== \"radio\"", block,
            "every radio station is live, so the label has to be dropped for "
            "radio or it covers up the bitrate on every single row")


class TheThumbLivesOnTheTrack(unittest.TestCase):
    """The volume thumb floated above the bar it belonged to.

    The knob was a sibling of the groove and centred itself with
    `y: (groove.height - height) / 2`. That arithmetic is correct for a child
    of the groove and wrong for a sibling: as a sibling it is positioned
    against the 24px hit strip instead of the 3px groove, so it sat about ten
    pixels too high. It lined up at one exact volume and nowhere else, which
    looks precisely like a slider you have to aim at very carefully.

    Position by hand against another object is the mistake. Nesting the knob
    inside the groove makes both coordinates come from the same parent, so the
    two cannot drift apart. This test is the structural half of that: it fails
    if the knob ever moves back out to being a sibling.

    The fill is checked the same way, since it was the original offender and
    the same edit could reintroduce it.
    """

    def test_knob_and_fill_are_children_of_the_groove(self):
        src = read(os.path.join(ROOT, "VolumeControl.qml"))
        groove = balanced_block(src, "id: groove")
        for child in ("id: knob", "width: parent.width * root.level"):
            self.assertIn(
                child, groove,
                "the volume fill and thumb must be nested inside the groove so "
                "their geometry is derived from the same parent; found "
                "outside it, which is how the thumb ended up floating above "
                "the track")

    def test_the_thumb_is_not_hidden_when_muted(self):
        """A thumb that disappears is a control you have to guess at."""
        src = read(os.path.join(ROOT, "VolumeControl.qml"))
        knob = balanced_block(src, "id: knob")
        self.assertNotIn(
            "visible:", knob,
            "the volume thumb must not be hidden when muted; dimming it "
            "keeps the control's position readable")
        self.assertIn("opacity:", knob,
                      "muting should dim the thumb, not remove it")

    def test_the_track_follows_the_shell_slider_convention(self):
        """Right-click a track to mute, as the shell's own slider does."""
        src = read(os.path.join(ROOT, "VolumeControl.qml"))
        area = balanced_block(src, "id: hover")
        self.assertIn("Qt.RightButton", area,
                      "the volume track must accept a right click, which is "
                      "how the shell's own panel slider mutes")
        self.assertIn("toggleMute", area,
                      "right-clicking the volume track must mute")

    def test_a_right_click_still_does_not_start_a_drag(self):
        """The drag is a left-button gesture and must stay one."""
        src = read(os.path.join(ROOT, "VolumeControl.qml"))
        area = balanced_block(src, "id: hover")
        self.assertIn("event.button !== Qt.LeftButton", area,
                      "pressing with any other button must not begin a drag, "
                      "or muting also scrubs the volume")


class NoDebugLeftovers(unittest.TestCase):
    def test_no_console_warn_outside_the_daemon_channel(self):
        allowed = re.compile(r'console\.warn\(\s*"ap-ctl:"')
        bad = []
        for path in files():
            for number, line in enumerate(read(path).splitlines(), 1):
                if "console.warn" in line and not allowed.search(line):
                    bad.append("%s:%d %s" % (os.path.basename(path), number,
                                             line.strip()[:70]))
        self.assertEqual(bad, [], "debug logging left in place:\n"
                         + "\n".join(bad))


class Manifest(unittest.TestCase):
    def test_manifest_matches_the_entry_point(self):
        import json
        with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        for kind, entry in manifest["entryPoints"].items():
            name = entry if entry.endswith(".qml") else entry + ".qml"
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)),
                            "manifest points at a missing %s (%s)" % (name, kind))

    def test_manifest_validates(self):
        if not HAVE_QMLLINT:
            self.skipTest("qmllint not present")
        proc = subprocess.run(["omarchy", "plugin", "validate", ROOT],
                              capture_output=True)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout.decode() + proc.stderr.decode())


class SourceIconsFollowTheSource(unittest.TestCase):
    """The bar icon has to say what is playing.

    Two things went wrong here and neither produced an error. `local` used a
    folder glyph, which describes where the file is rather than what is
    playing, so local music was indistinguishable from browsing a directory.
    And every icon asked for the family "Nerd Font", which this system does
    not have: `fc-match "Nerd Font"` answers Liberation Sans, a font with none
    of the glyphs in it. Qt fell back silently and drew a substitute or
    nothing at all, so the icons could look right in the source and be blank
    on the bar. The shell's own convention is `Style.font.family`, which
    follows the omarchy font alias.
    """

    SOURCES = ("radio", "tv", "youtube", "music", "local")

    def _model(self):
        with open(os.path.join(ROOT, "Model.js"), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_every_source_has_an_entry(self):
        model = self._model()
        block = re.search(r"var SOURCE = \{(.*?)\n\}", model, re.S)
        self.assertIsNotNone(block, "Model.js has no SOURCE map")
        for name in self.SOURCES:
            self.assertRegex(block.group(1), r"(?m)^\s*%s:\s*\{" % name,
                             "%s has no icon entry, so it falls back to a "
                             "generic glyph" % name)

    def test_local_music_is_a_note_not_a_folder(self):
        model = self._model()
        entry = re.search(r"(?m)^\s*local:\s*\{[^}]*\}", model)
        self.assertIsNotNone(entry)
        self.assertIn("ICON.music", entry.group(0),
                      "local music should show a note; a folder describes the "
                      "file, not the sound")

    def test_no_file_asks_for_a_font_family_that_does_not_exist(self):
        """`fc-match` is the authority on which family actually resolves.

        Comparing the requested name against the resolved *family* is the
        whole point: the file name always differs, so a naive comparison passes
        for a family that does not exist at all. Generic CSS families are
        allowed to resolve elsewhere - that is how the omarchy font alias
        works - everything else has to resolve to itself or Qt is silently
        substituting a font with none of the icon glyphs in it.
        """
        generic = {"monospace", "sans-serif", "serif", "system-ui", "cursive",
                   "fantasy", "inherit"}
        # Covers both `font.family: "X"` and a property whose default is a
        # family name, such as `fontFamilyOverride: "X"`.
        pattern = re.compile(r'(?:font\.family|fontFamilyOverride):\s*"([^"]+)"')
        for path in files():
            for match in pattern.finditer(read(path)):
                family = match.group(1)
                if family.lower() in generic:
                    continue
                proc = subprocess.run(["fc-match", family], capture_output=True,
                                      text=True)
                quoted = re.search(r'"([^"]+)"', proc.stdout or "")
                resolved = quoted.group(1) if quoted else ""
                self.assertEqual(
                    resolved, family,
                    "%s asks for font family %r but fontconfig resolves it to "
                    "%r, so any icon glyphs in it will silently fall back"
                    % (os.path.basename(path), family, resolved))


@unittest.skipUnless(HAVE_QMLLINT, "qmllint not present")
class Lint(unittest.TestCase):
    def test_every_file_lints(self):
        bad = []
        for path in files():
            proc = subprocess.run(
                ["qmllint", "-I", "/usr/share/omarchy/shell", "-I", ROOT, path],
                capture_output=True, text=True)
            if proc.stdout.strip() or proc.stderr.strip():
                bad.append("%s\n%s%s" % (os.path.basename(path), proc.stdout,
                                         proc.stderr))
        self.assertEqual(bad, [], "qmllint reported problems:\n" + "\n".join(bad))


_QML_OPENER = re.compile(r"^\s*(?:[\w.]+\s*:\s*)?[A-Z][\w.]*\s*\{")
_QML_KEY = re.compile(r"^\s*(?:(?:readonly\s+|required\s+|default\s+)*property\s+[\w<>.]+\s+(\w+)"
                      r"|((?:on[A-Z]\w*)|(?:[a-z]\w*(?:\.\w+)*))\s*:)")
_STRINGS = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')


def duplicate_members(src):
    """Bindings, handlers and properties declared twice in one QML object.

    The shell refuses the whole file for that ("Property value set multiple
    times"), and qmllint does not notice it - which is how a second
    onSourceChanged once took the panel down at load. Only QML object scopes
    are checked; JavaScript blocks are skipped.
    """
    found = []
    stack = []                       # one entry per open brace: a set, or None for JS
    for number, raw in enumerate(src.splitlines(), 1):
        line = raw.strip()
        if line.startswith("//") or line.startswith("*") or line.startswith("/*"):
            continue
        bare = _STRINGS.sub('""', raw.split(" //")[0])
        if stack and stack[-1] is not None:
            match = _QML_KEY.match(bare)
            if match:
                key = match.group(1) or match.group(2)
                if key in stack[-1]:
                    found.append("%d: %s" % (number, key))
                stack[-1].add(key)
        opener = bool(_QML_OPENER.match(bare))
        for char in bare:
            if char == "{":
                stack.append(set() if opener else None)
                opener = False
            elif char == "}" and stack:
                stack.pop()
    return found


class TooltipsFollowTheTheme(unittest.TestCase):
    """Qt's stock ToolTip ignores the Omarchy theme and comes out pale
    yellow; every tooltip goes through ThemedToolTip (or a shell control's
    own tooltipText)."""

    def test_no_stock_tooltips(self):
        bad = []
        for path in files():
            if os.path.basename(path) == "ThemedToolTip.qml":
                continue
            src = read(path)
            for number, line in enumerate(src.splitlines(), 1):
                if re.search(r"\bToolTip\.(visible|text)\b|^\s*ToolTip\s*\{", line):
                    bad.append("%s:%d" % (os.path.basename(path), number))
        self.assertEqual(bad, [])


class NoDuplicateMembers(unittest.TestCase):
    def test_nothing_is_declared_twice_in_one_object(self):
        bad = []
        for path in files():
            for where in duplicate_members(read(path)):
                bad.append("%s:%s" % (os.path.basename(path), where))
        self.assertEqual(bad, [])

    def test_the_check_catches_the_real_mistake(self):
        src = "Item {\n  onSourceChanged: a()\n  Text {\n    onSourceChanged: b()\n  }\n" \
              "  onSourceChanged: {\n    c()\n  }\n}\n"
        self.assertEqual(duplicate_members(src), ["6: onSourceChanged"])


class TheStatusBarReportsWhenTheListWasDownloaded(unittest.TestCase):
    """Radio and television read from the mirror, so say when it was filled.

    Without this the list on screen has an unknown age: the same station rows
    could have come from a download last week or last month, and there is
    nothing on screen to tell them apart.
    """

    def _browser(self):
        return read(os.path.join(ROOT, "Browser.qml"))

    def test_the_footer_carries_the_download_time(self):
        src = self._browser()
        self.assertIn("relativeAge(root.updatedAgo)", src,
                      "the status bar must show how long ago the mirror was "
                      "downloaded")
        self.assertIn(root_source := 'root.source + "_age"', src,
                      "each tab reads its own timestamp, so radio and "
                      "television cannot show each other's")

    def test_the_age_is_never_invented(self):
        """Missing is unknown. Saying 'just now' would be a reassurance."""
        src = self._browser()
        self.assertIn('return "unknown"', src,
                      "a timestamp that was never written must read as "
                      "unknown rather than as freshly downloaded")
        self.assertIn('!== undefined', src,
                      "the age has to be able to be absent; defaulting it to 0 "
                      "turns 'never downloaded' into 'just now'")

    def test_no_unit_can_round_up_into_the_next_one(self):
        src = self._browser()
        for line in src.splitlines():
            if "Math.round(" in line and ("/ 60" in line or "/ 3600" in line
                                          or "/ 86400" in line):
                self.fail("rounding an age makes it read '60 min ago' or "
                          "'24 h ago' at the boundary: %s" % line.strip())

    def test_only_the_mirrored_tabs_show_an_age(self):
        """YouTube and local music are not downloaded, so they have no date."""
        src = self._browser()
        self.assertIn("mirroredSource", src,
                      "the status bar must be limited to the sources that "
                      "actually have a download behind them")
        guarded = re.search(r"visible: root\.mirroredSource", src)
        self.assertIsNotNone(guarded,
                             "the update line is for radio and television only")

    def test_the_count_is_the_number_behind_the_list(self):
        src = self._browser()
        self.assertIn("mirroredCount", src,
                      "the status bar should say how much was downloaded, so "
                      "an empty-looking tab can be told from an empty mirror")

    def test_the_update_button_asks_for_one_source(self):
        panel = read(os.path.join(ROOT, "Panel.qml"))
        self.assertIn("updateCatalogue(root.source)", self._browser(),
                      "the button must name the tab it is refreshing")
        self.assertIn("source: src", panel,
                      "a refresh has to be scoped: updating the radio tab "
                      "should not silently re-download television as well")

    def test_a_landed_mirror_reaches_the_open_tab(self):
        """The timestamp and the rows have to be the same moment."""
        self.assertIn('{"type": "catalogue"', read(
            os.path.join(ROOT, "src/apctl/core/daemon.py")),
            "the daemon must announce a finished mirror, or the footer shows "
            "a time older than the list it sits under")


class TheStatusBarIsStaticAndSaysWhatItKnows(unittest.TestCase):
    """Radio and television need a line that is always there and always true.

    The list scrolls; the status bar does not. It has to say how much is
    downloaded, how much of it the current filter selects, and when it arrived
    - including while a download is running, when "60 results" would be
    describing rows that are in the middle of being replaced.
    """

    def _browser(self):
        return read(os.path.join(ROOT, "Browser.qml"))

    def test_the_status_bar_is_outside_the_scrolling_list(self):
        """Pinned below the list, not inside it."""
        src = self._browser()
        list_start = src.index("id: list")
        list_end = src.index("// ---- status bar")
        self.assertLess(list_start, list_end,
                        "the status bar has to come after the list in the "
                        "layout, or the list pushes it off the bottom")
        self.assertIn("Layout.fillHeight: true", src[:list_start],
                      "the list takes the slack so the bar keeps its place "
                      "instead of the two scrolling together")

    def test_the_bar_is_there_even_with_nothing_in_the_list(self):
        src = self._browser()
        bar = balanced_block(src, "// ---- status bar")
        self.assertNotIn(
            "root.items.length > 0", bar.split("play all")[0],
            "the bar describes the catalogue, not the page. Hiding it when the "
            "page is empty removes the status exactly when it is needed most")

    def test_it_reports_the_download_total_and_the_filter_size(self):
        src = self._browser()
        self.assertIn("stockLine()", src,
                      "the total downloaded and its age belong on the bar")
        self.assertIn("filterLine()", src,
                      "which filter produced the list has to be stated, or "
                      "'60 results' could be any of three different lists")
        self.assertIn("root.total", src,
                      "the size of the filter, not just the size of the page")

    def test_the_filter_line_names_every_active_filter(self):
        src = self._browser()
        for prop in ("genreFilter", "countryText", "searchText"):
            self.assertIn(prop, src.split("activeFilters")[1][:600],
                          "%s is an active filter and has to be named" % prop)

    def test_a_download_replaces_the_line_with_its_own_progress(self):
        src = self._browser()
        self.assertIn("syncLine()", src)
        self.assertIn("downloading", src,
                      "a download in progress has to be visible as one")
        self.assertIn("stage", src,
                      "download and parse are different work; a bar that sits "
                      "at 100% through the parse is a lie")

    def test_progress_shows_rows_bytes_and_a_percentage(self):
        src = self._browser()
        line = balanced_block(src, "function syncLine()", opening="after")
        self.assertIn("Model.count(rows", line,
                      "how many rows are in")
        self.assertIn("Model.bytes(info.bytes)", line,
                      "how much has been downloaded")
        self.assertIn('+ "%"', line,
                      "and how far along that is, against the real total")
        self.assertIn("Model.count(total", line,
                      "the denominator has to be printed, so the percentage "
                      "can be checked against it")

    def test_no_percentage_without_a_real_total(self):
        src = self._browser()
        line = balanced_block(src, "function syncLine()", opening="after")
        guarded = re.search(r"if \(info\.stage === \"download\" && total\)", line)
        self.assertIsNotNone(guarded,
                             "a percentage needs a denominator; without one "
                             "the bar would pass 100% and keep going")

    def test_the_percentage_measures_the_same_thing_as_the_total(self):
        """Radio counts stations, television counts playlist bytes.

        Measuring bytes against a station total gave a finished 2.5 MB download
        a reading of 0%, which is worse than showing nothing.
        """
        src = self._browser()
        line = balanced_block(src, "function syncLine()", opening="after")
        self.assertIn('info.unit === "bytes"', line,
                      "the numerator has to be chosen to match the denominator")
        self.assertIn("var done = byBytes", line,
                      "one number has to drive the percentage, and it has to "
                      "be the right one for this source")
        pct = re.search(r"\(done / total\)", line)
        self.assertIsNotNone(pct,
                             "the percentage must be computed from `done`, "
                             "not from rows directly")

    def test_the_empty_catalogue_offers_a_way_out(self):
        src = self._browser()
        self.assertIn("has not been downloaded yet", src)
        self.assertIn("updateCatalogue(root.source)", src,
                      "an empty catalogue needs a button, not just a sentence")


class CountsAreReadable(unittest.TestCase):
    def test_thousands_are_separated(self):
        """59826 is not a number anyone reads at a glance."""
        model_path = os.path.join(ROOT, "Model.js")
        self.assertIn("function count(", read(model_path))
        out = subprocess.run(
            ["node", "-e",
             'const fs=require("fs");'
             'const src=fs.readFileSync(process.argv[1],"utf8");'
             'eval(/function count\\(.*?\\n\\}/s.exec(src)[0]);'
             'console.log([count(0,"station","stations"),'
             'count(1,"station","stations"),'
             'count(999,"station","stations"),'
             'count(1000,"station","stations"),'
             'count(59826,"station","stations"),'
             'count(11014,"channel","channels")].join("|"))',
             model_path],
            capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(),
                         "0 stations|1 station|999 stations|1,000 stations|"
                         "59,826 stations|11,014 channels",
                         "a catalogue count has to be legible at a glance")



class InstallerSearch(unittest.TestCase):
    def test_the_copied_search_finds_exactly_the_missing_packages(self):
        """Omarchy's package picker is an fzf list, where a space means "and":
        "mpv bubblewrap" matched nothing. The copied search uses fzf's own
        exact-name and "or" syntax instead."""
        model_path = os.path.join(ROOT, "Model.js")
        out = subprocess.run(
            ["node", "-e",
             'const fs=require("fs");'
             'const src=fs.readFileSync(process.argv[1],"utf8");'
             'eval(/function installerSearch\\(.*?\\n\\}/s.exec(src)[0]);'
             'console.log([installerSearch(["mpv","bubblewrap"]),'
             'installerSearch(["yt-dlp"]), installerSearch([])].join("|"))',
             model_path],
            capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "^mpv$ | ^bubblewrap$|^yt-dlp$|")
        if shutil.which("fzf"):
            listing = "mpv\nmpv-mpris\nbubblewrap\nbubblewrap-suid\nyt-dlp\nyt-dlp-ejs\n"
            found = subprocess.run(["fzf", "--filter", "^mpv$ | ^bubblewrap$"], input=listing,
                                   capture_output=True, text=True).stdout.split()
            self.assertEqual(sorted(found), ["bubblewrap", "mpv"])



if __name__ == "__main__":
    unittest.main(verbosity=2)

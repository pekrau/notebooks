"Simple app for personal notes. Optionally publish using GitHub pages."

__version__ = "0.3.5"

import collections
import json
import logging
import os
import time

import flask
import marko
import marko.ast_renderer
import jinja2.utils


ROOT = None           # Created in 'setup'
LOOKUP = dict()       # path->note lookup
RECENT = None         # Created in 'setup'
STARRED = set()       # Notes
BACKLINKS = dict()    # target note path -> set of source target paths

SETTINGS = dict(VERSION = __version__,
                SERVER_NAME = "localhost:5099",
                SECRET_KEY = "this is a secret key",
                TEMPLATES_AUTO_RELOAD = True,
                DEBUG = True,
                JSON_AS_ASCII = False,
                NOTES_DIRPATH = "notes",
                MAX_RECENT = 10)


class Note:
    "Note and its subnotes, if any."

    def __init__(self, supernote, title):
        self.supernote = supernote
        if supernote:
            supernote.subnotes.append(self)
        self.subnotes = []
        self._title = title
        self._text = ""
        self.modified = None

    def __repr__(self):
        return self.path

    def __lt__(self, other):
        return self.title < other.title

    def get_title(self):
        return self._title

    def set_title(self, title):
        """Set a new title, which changes its path.
        Updates notes that link to this note or its subnotes.
        Raise ValueError if the title is invalid; bad start or end characters.
        Raise KeyError if there is already a note with that title
        """
        if not self.supernote: return  # Root note has no title to change.
        title = title.strip()
        if not title: raise ValueError
        if "/" in title: raise ValueError
        if title[0] == ".": raise ValueError
        if title[0] == "_": raise ValueError
        if title[-1] == "~": raise ValueError
        if self._title == title: return
        new_abspath = os.path.join(SETTINGS["NOTES_DIRPATH"],
                                   self.supernote.path,
                                   title)
        if os.path.exists(new_abspath): raise KeyError
        if os.path.exists(f"{new_abspath}.md"): raise KeyError
        print("old title:", self.title, "new title:", title)
        # The set of notes whose paths will change: this one and all below it.
        changing = list(self.traverse())
        print("changing:", changing)
        # The set of notes which link to any of these changed-path notes.
        affected = set()
        for note in changing:
            try:
                affected.update(BACKLINKS[note.path])
            except KeyError:
                pass
        affected = [LOOKUP[p] for p in affected]
        # Remove all backlinks while old paths.
        for note in affected:
            note.remove_backlinks()
        # Remember the old path for each note whose paths will change.
        old_paths = [n.path for n in changing]
        # Remove this note and below from the path->note lookup while old path.
        for note in changing:
            note.remove_lookup()
        # Old abspath needed for renaming directory/file.
        old_abspath = self.abspath
        # Actually change the title of the note.
        self._title = title
        if os.path.isdir(old_abspath):
            abspath = self.abspath
            print("rename dir:", old_abspath, abspath)
            os.rename(old_abspath, abspath)  # New abspath
        else:
            abspath = f"{self.abspath}.md"
            print("rename file:", f"{old_abspath}.md", abspath)
            os.rename(f"{old_abspath}.md", abspath)
        # Update modified timestamp and add note to recently changed.
        self.modified = os.path.getmtime(abspath)
        put_recent(self)
        # Add this note and below to path->note lookup with new path.
        for note in changing:
            note.add_lookup()
        # Get the new path for each note whose path was changed.
        changed_paths = zip(old_paths, [n.path for n in changing])
        for note in affected:
            text = note.text
            for old_path, new_path in changed_paths:
                text = text.replace(f"[[{old_path}]]", f"[[{new_path}]]")
            note.text = text
            note.write()
        # Add back backlinks with new paths in place.
        for note in affected:
            note.add_backlinks()

    title = property(get_title, set_title, doc="The title of the note.")

    def get_text(self):
        return self._text

    def set_text(self, text):
        self.remove_backlinks()
        self._text = text
        self.add_backlinks()
        self.write()

    text = property(get_text, set_text,
                    doc="The text of the note using Markdown format.")

    def get_linkpaths(self, children):
        """Find the note links in the AST tree.
        Return the set of paths for the notes linked to.
        """
        result = set()
        if isinstance(children, list):
            for child in children:
                if child.get("element") == "note_link":
                    result.add(child["ref"])
                try:
                    result.update(self.get_linkpaths(child["children"]))
                except KeyError:
                    pass
        return result

    @property
    def path(self):
        "Return the path of the note."
        if self.supernote:
            assert self.title
            return os.path.join(self.supernote.path, self.title)
        else:
            return self.title or ""

    @property
    def idpath(self):
        "Return the path of the note as a valid HTML code identifier."
        return self.path.replace("/", "-").replace(" ", "_").replace(",", "_")

    @property
    def abspath(self):
        "Return the absolute filepath of the note."
        path = self.path
        if path:
            return  os.path.join(SETTINGS["NOTES_DIRPATH"], path)
        else:
            return SETTINGS["NOTES_DIRPATH"]

    @property
    def url(self):
        if self.path:
            return flask.url_for("note", path=self.path)
        else:
            return flask.url_for("home")

    @property
    def count(self):
        "Return the number of subnotes."
        return len(self.subnotes)

    @property
    def starred(self):
        "Is the note starred?"
        return self in STARRED

    def star(self, remove=False):
        "Toggle the star state of the note, or force remove."
        if self in STARRED:
            STARRED.remove(self)
        elif not remove:
            STARRED.add(self)
        else:
            return              # No change; no need to update file.
        filepath = os.path.join(SETTINGS["NOTES_DIRPATH"], "__starred__.json")
        with open(filepath, "w") as outfile:
            json.dump({"paths": [n.path for n in STARRED]}, outfile)

    def count_traverse(self):
        "Return the number of subnotes recursively, including this one."
        result = 1
        for subnote in self.subnotes:
            result += subnote.count_traverse()
        return result

    def supernotes(self):
        "Return the list of supernotes."
        if self.supernote:
            return self.supernote.supernotes() + [self.supernote]
        else:
            return []

    def traverse(self):
        "Return a generator traversing this note and its subnotes."
        yield self
        for subnote in self.subnotes:
            yield from subnote.traverse()

    def write(self):
        "Write this note to disk. Does *not* write subnotes."
        if os.path.isdir(self.abspath):
            abspath = os.path.join(self.abspath, "__text__.md")
            with open(abspath, "w") as outfile:
                outfile.write(self.text)
        else:
            abspath = f"{self.abspath}.md"
            with open(abspath, "w") as outfile:
                outfile.write(self.text)
        self.modified = os.path.getmtime(abspath)
        put_recent(self)

    def read(self):
        "Read this note and its subnotes from disk."
        abspath = self.abspath
        if os.path.exists(abspath):
            # It's a directory with subnotes.
            try:
                filepath = os.path.join(abspath, "__text__.md")
                with open(filepath) as infile:
                    self._text = infile.read()
                self.modified = os.path.getmtime(filepath)
            except OSError:
                self._text = ""
                self.modified = os.path.getmtime(abspath)
            for filename in sorted(os.listdir(abspath)):
                if filename.startswith("."): continue
                if filename.startswith("_"): continue
                if filename.endswith("~"): continue
                note = Note(self, os.path.splitext(filename)[0])
                note.read()
        else:
            # It's a file; no subnotes.
            filepath = f"{abspath}.md"
            with open(filepath) as infile:
                self._text = infile.read()
            self.modified = os.path.getmtime(filepath)

    def add_lookup(self):
        "Add the note to the path->note lookup."
        LOOKUP[self.path] = self

    def remove_lookup(self, path=None):
        "Remove the note from the path->note lookup."
        LOOKUP.pop(path or self.path)

    def get_backlinks(self):
        "Get the notes linking to this note."
        return sorted([LOOKUP[p] for p in BACKLINKS.get(self.path, [])])

    def add_backlinks(self):
        "Add the links from this text to other notes."
        ast = MARKDOWN_AST.convert(self.text)
        linkpaths = self.get_linkpaths(ast["children"])
        path = self.path
        for link in linkpaths:
            BACKLINKS.setdefault(link, set()).add(path)

    def remove_backlinks(self):
        "Remove the links from this note to other notes."
        ast = MARKDOWN_AST.convert(self.text)
        linkpaths = self.get_linkpaths(ast["children"])
        path = self.path
        for link in linkpaths:
            try:
                BACKLINKS[link].remove(path)
            except KeyError:    # When stale link.
                pass

    def create_subnote(self, title, text):
        "Create and return a subnote."
        if title in self.subnotes:
            raise ValueError(f"Note already exists: '{title}'")
        if os.path.isfile(f"{self.abspath}.md"):
            abspath = self.abspath
            os.mkdir(abspath)
            os.rename(f"{abspath}.md", os.path.join(abspath, "__text__.md"))
        note = Note(self, title)
        note.text = text        # This also adds backlinks.
        self.subnotes.sort()
        note.write()
        note.add_lookup()
        return note

    def is_deletable(self):
        """May this note be deleted?
        - Must have no subnotes.
        - Must have no links to it.
        """
        if self.supernote is None: return False
        if self.count: return False
        if self.get_backlinks(): return False
        return True

    def delete(self):
        "Delete this note."
        if not self.is_deletable():
            raise ValueError("This note may not be deleted.")
        self.remove_lookup()
        self.remove_backlinks()
        self.star(remove=True)
        os.remove(f"{self.abspath}.md")
        self.supernote.subnotes.remove(self)
        # Convert supernote to file if no subnotes any longer. Not root!
        if self.supernote.count == 0 and self.supernote is not None:
            abspath = self.supernote.abspath
            filepath = os.path.join(abspath, "__text__.md")
            try:
                os.rename(filepath, f"{abspath}.md")
            except OSError:     # May happen if dir/files created externally.
                with open(f"{abspath}.md", "w") as outfile:
                    outfile.write(sels.supernote.text)
            os.rmdir(abspath)

    def get_tree(self):
        "Get the note contents as a dict."
        result = {"title": self.title,
                  "path": self.path,
                  "text": self.text,
                  "subnotes": [s.get_tree() for s in self.subnotes]}
        if self.supernote:
            result["supernote"] = self.supernote.path
        return result


class NoteLink(marko.inline.InlineElement):
    pattern = r'\[\[ *(.+?) *\]\]'
    parse_children = False
    def __init__(self, match):
        self.ref = match.group(1)

class NoteLinkRendererMixin:
    def render_note_link(self, element):
        try:
            note = LOOKUP[element.ref]
        except KeyError:
            # Stale link; target does not exist.
            return f'<span class="text-danger">{element.ref}</span>'
        else:
            # Proper link to target.
            return f'<a class="fw-bold text-decoration-none" href="{note.url}">{note.title}</a>'

class NoteLinkExt:
    elements = [NoteLink]
    renderer_mixins = [NoteLinkRendererMixin]

MARKDOWN = marko.Markdown(extensions=[NoteLinkExt])
MARKDOWN_AST = marko.Markdown(extensions=[NoteLinkExt],
                              renderer=marko.ast_renderer.ASTRenderer)

def markdown(value):
    "Filter to process the value using augmented Marko markdown."
    return jinja2.utils.Markup(MARKDOWN.convert(value or ""))

def localtime(value):
    "Filter to convert epoch value to local time ISO string."
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))

def flash_error(msg): flask.flash(str(msg), "error")

def flash_warning(msg): flask.flash(str(msg), "warning")

def flash_message(msg): flask.flash(str(msg), "message")

def get_starred(): return sorted(STARRED)

def get_recent(): return list(RECENT)


app = flask.Flask(__name__)

app.add_template_filter(markdown)
app.add_template_filter(localtime)

@app.before_first_request
def setup():
    """Read all notes and keep in memory. Set up:
    - Lookup:  path->note
    - List of recent notes
    - List of starred notes
    - Set up map of backlinks
    """
    global ROOT
    global RECENT
    # Read in all notes, add to the path->note lookup.
    ROOT = Note(None, None)
    ROOT.read()
    traverser = ROOT.traverse()
    next(traverser)             # Skip root note.
    for note in traverser:
        note.add_lookup()
    # Set up most recently modified notes.
    traverser = ROOT.traverse()
    next(traverser)             # Skip root note.
    notes = list(traverser)     # XXX simple but not very good
    notes.sort(key=lambda n: n.modified, reverse=True)
    RECENT = collections.deque(notes[:SETTINGS["MAX_RECENT"]],
                               maxlen=SETTINGS["MAX_RECENT"])
    # Get the starred notes.
    try:
        filepath = os.path.join(SETTINGS["NOTES_DIRPATH"], "__starred__.json")
        with open(filepath) as infile:
            STARRED.update([LOOKUP[p] for p in json.load(infile)["paths"]])
    except OSError:
        pass
    # Set up the backlinks for all notes.
    for note in ROOT.traverse():
        note.add_backlinks()

def put_recent(note):
    "Put the note to the start of the list of recently modified notes."
    # Root note should not be listed.
    if note.supernote is not None: return
    try:
        RECENT.remove(note)
    except ValueError:
        pass
    RECENT.appendleft(note)
    assert check_recent()

def check_recent():
    latest = RECENT[0]
    for note in RECENT:
        if note.modified > latest.modified:
            print(f"RECENT out of order: '{note}', '{latest}'")
            for n in RECENT:
                print(n, localtime(n.modified))
            return False
        latest = note
    return True

@app.context_processor
def setup_template_context():
    "Add to the global context of Jinja2 templates."
    return dict(interactive=True,
                flash_error=flash_error,
                flash_warning=flash_warning,
                flash_message=flash_message,
                get_recent=get_recent,
                get_starred=get_starred)


@app.route("/")
def home():
    "Home page; root note."
    n_links = sum([len(s) for s in BACKLINKS.values()])
    return flask.render_template("home.html", root=ROOT, n_links=n_links)

@app.route("/note")
@app.route("/note/")
def root():
    "Root note is shown in the home page."
    return flask.redirect(flask.url_for("home"))

@app.route("/create", methods=["GET", "POST"])
def create():
    "Create a new note."
    if flask.request.method == "GET":
        try:
            supernote = LOOKUP[flask.request.values["supernote"]]
        except KeyError:
            supernote = None    # Root supernote.
        try:
            source = LOOKUP[flask.request.values["source"]]
        except KeyError:
            source = None
        return flask.render_template("create.html",
                                     supernote=supernote,
                                     source=source)

    elif flask.request.method == "POST":
        try:
            superpath = flask.request.form["supernote"]
            if not superpath: raise KeyError
        except KeyError:
            supernote = ROOT
        else:
            try:
                supernote = LOOKUP[superpath]
            except KeyError:
                raise
                flash_error(f"No such supernote: '{superpath}'")
                return flask.redirect(flask.url_for("home"))
        title = flask.request.form.get("title") or "No title"
        title = title.replace("\n", " ")  # Clean up title.
        title = title.replace("/", " ")
        title = title.strip()
        title = title.lstrip(".")
        title = title.lstrip("_")
        text = flask.request.form.get("text") or ""
        try:
            note = supernote.create_subnote(title=title, text=text)
        except ValueError as error:
            flash_error(error)
            return flask.redirect(supernote.url)
        return flask.redirect(note.url)

@app.route("/note/<path:path>")
def note(path):
    "Display page for the given note."
    try:
        note = LOOKUP[path]
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))
    return flask.render_template("note.html", note=note)

@app.route("/edit/", methods=["GET", "POST"])
@app.route("/edit/<path:path>", methods=["GET", "POST"])
def edit(path=""):
    "Edit the given note; title (i.e. file/directory rename) and/or text."
    try:
        note = LOOKUP[path]
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))

    if flask.request.method == "GET":
        return flask.render_template("edit.html", note=note)

    elif flask.request.method == "POST":
        try:
            title = flask.request.form.get("title") or ""
            note.title = title
        except ValueError:
            flash_error(f"Invalid title: '{title}'")
            return flask.redirect(flask.url_for("edit", path=path))
        except KeyError:
            flash_error(f"Note already exists: '{title}'")
            return flask.redirect(flask.url_for("edit", path=path))
        note.text = flask.request.form.get("text") or ''
        return flask.redirect(note.url)

@app.route("/star/<path:path>", methods=["POST"])
def star(path):
    "Toggle the star state of the note for the path."
    try:
        note = LOOKUP[path]
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))
    note.star()
    return flask.redirect(note.url)

@app.route("/delete/<path:path>", methods=["POST"])
def delete(path):
    "Delete the given note."
    try:
        note = LOOKUP[path]
    except KeyError:
        flash_error(f"No such note: '{path}'")
        return flask.redirect(flask.url_for("note", path=os.path.dirname(path)))
    try:
        note.delete()
    except ValueError as error:
        flash_error(error)
        return flask.redirect(note.url)
    return flask.redirect(note.supernote.url)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        dirpath = os.path.expanduser(sys.argv[1])
        dirpath = os.path.normpath(dirpath)
    else:
        dirpath = os.path.join(os.getcwd(), "notes")
    if not os.path.exists(dirpath):
        sys.exit(f"No such directory: {dirpath}")
    if not os.path.isdir(dirpath):
        sys.exit(f"Not a directory: {dirpath}")
    try:
        filepath = os.path.join(dirpath, ".settings.json")
        with open(filepath) as infile:
            SETTINGS.update(json.load(infile))
        SETTINGS["SETTINGS_FILEPATH"] = filepath
        logging.info(f"Settings file: {filepath}")
    except OSError:
        SETTINGS["SETTINGS_FILEPATH"] = None
    SETTINGS["NOTES_DIRPATH"] = dirpath
    app.config.from_mapping(SETTINGS)
    app.run(debug=True)

"""Minimal curses and screen fakes for modal tests (were in test_control_tui_focus)."""


class FakeCurses:
    KEY_RESIZE = 410
    KEY_UP = 259
    KEY_DOWN = 258
    KEY_ENTER = 343
    KEY_BACKSPACE = 263
    KEY_BTAB = 353
    A_BOLD = 1
    A_REVERSE = 2
    A_DIM = 4
    A_UNDERLINE = 8
    error = RuntimeError

    def __init__(self, cursor=0):
        self.cursor = cursor
        self.cursor_changes: list[int] = []

    def curs_set(self, visibility):
        previous = self.cursor
        self.cursor = visibility
        self.cursor_changes.append(visibility)
        return previous


class FakeScreen:
    def __init__(self, keys=(), *, height=18, width=80):
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.writes: list[tuple[int, int, str, int, int]] = []
        self.cursor = None

    def getmaxyx(self):
        return self.height, self.width

    def get_wch(self):
        return self.keys.pop(0)

    def getch(self):
        return self.keys.pop(0)

    def erase(self):
        pass

    def move(self, y, x):
        self.cursor = (y, x)

    def clrtoeol(self):
        pass

    def addnstr(self, y, x, value, limit, attribute=0):
        self.writes.append((y, x, value, limit, attribute))

    def refresh(self):
        pass

    def timeout(self, _value):
        pass

    @property
    def text(self):
        return "\n".join(item[2] for item in self.writes)



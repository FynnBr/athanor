import math
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from athanor import cli, tarot, wall


@pytest.fixture
def terminal(monkeypatch, capsys):
    monkeypatch.delenv("ATHANOR_SIXEL", raising=False)
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(type(cli.sys.stdout), "isatty", lambda self: True)


@pytest.mark.parametrize(
    ("term", "window", "tty", "expected"),
    [
        ("foot", "", True, "sixel"),
        ("foot-extra", "", True, "sixel"),
        ("xterm-kitty", "", True, "kitty"),
        ("xterm-256color", "1", True, "kitty"),
        ("xterm", "", True, None),
        ("foot", "", False, None),
        ("xterm-kitty", "1", False, None),
    ],
)
def test_graphics_protocol(terminal, monkeypatch, term, window, tty, expected):
    monkeypatch.setenv("TERM", term)
    monkeypatch.setenv("KITTY_WINDOW_ID", window)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: tty)
    assert cli._graphics_protocol() == expected


@pytest.mark.parametrize(("override", "expected"), [("0", None), ("1", "sixel")])
def test_sixel_override_is_preserved(terminal, monkeypatch, override, expected):
    monkeypatch.setenv("TERM", "xterm-kitty")
    monkeypatch.setenv("ATHANOR_SIXEL", override)
    assert cli._graphics_protocol() == expected


@pytest.fixture
def rendering(terminal, tmp_path, monkeypatch):
    for card in tarot.DECK:
        tarot.art_path(card, tmp_path).touch()
    monkeypatch.setattr(cli, "_art_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.transmute, "saved_scheme", lambda: "umber")
    monkeypatch.setattr(cli.sigil, "host_digest", lambda: (b"host", "fingerprint"))
    monkeypatch.setattr(wall, "_magick", lambda: "magick")
    monkeypatch.setattr(wall, "_font", lambda: [])
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/kitten")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "magick":
            if args[-1].endswith(":-"):
                return SimpleNamespace(stdout=b"PNG" if args[-1] == "png:-" else b"SIXEL")
            Path(args[-1]).touch()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("term", ["foot", "xterm-kitty"])
@pytest.mark.parametrize(
    "args", [[], ["2"], ["3"], ["4"], ["5"], ["--day"], ["--card", "the tower, reversed"]]
)
def test_draw_renders_cards(rendering, monkeypatch, capsys, term, args):
    monkeypatch.setenv("TERM", term)
    cli.main(["draw", *args])
    output = capsys.readouterr().out
    assert output.strip()
    cards = [cmd for cmd, _ in rendering if cmd[0] == "magick" and cmd[1] != "montage"]
    assert len(cards) == (int(args[0]) if args and args[0].isdigit() else 1)
    assert all(cmd[-1].endswith(".png") for cmd in cards)
    if "--card" in args:
        assert "-rotate" in cards[0]
        assert "reversed" in output
    montage = next(cmd for cmd, _ in rendering if cmd[1] == "montage")
    if term == "foot":
        assert montage[-1] == "sixel:-"
        assert output.endswith("SIXEL\n")
        assert len(rendering) == len(cards) + 1
    else:
        assert montage[-1] == "png:-"
        command, kwargs = rendering[-1]
        assert command == [
            "/usr/bin/kitten", "icat", "--stdin=yes", "--transfer-mode=stream",
            "--unicode-placeholder",
        ]
        assert kwargs["input"] == b"PNG"
        assert kwargs["check"] is True
        assert "SIXEL" not in output


@pytest.mark.parametrize(
    ("term", "args"),
    [("xterm", []), ("foot", ["--no-art"]), ("xterm-kitty", ["--no-art"])],
)
def test_text_only_fallback(rendering, monkeypatch, capsys, term, args):
    monkeypatch.setenv("TERM", term)
    cli.main(["draw", "--card", "the tower", *args])
    output = capsys.readouterr().out
    assert "XVI The Tower" in output
    assert tarot.DECK[16].upright in output
    assert rendering == []


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("cell_height", [16, 24])
def test_kitty_reserves_all_image_rows(terminal, tmp_path, monkeypatch, n, cell_height):
    kitten = shutil.which("kitten")
    if not kitten:
        pytest.skip("kitten is not installed")
    monkeypatch.setenv("TERM", "xterm-kitty")
    monkeypatch.setattr(cli, "_art_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.transmute, "saved_scheme", lambda: "umber")
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda: SimpleNamespace(columns=60))
    for card in tarot.DECK:
        tarot.art_path(card, tmp_path).touch()
    monkeypatch.setattr(wall, "card_image", lambda src, scheme, rev, fmt, out, **kw: out)
    dimensions = []

    def spread(pngs, scheme, per_row, *, fmt):
        assert fmt == "png"
        assert per_row == min(2, n)
        width, height = per_row * 224, math.ceil(n / per_row) * 376
        dimensions.append((width, height))

        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff" * width) * height))
            + chunk(b"IEND", b"")
        )

    monkeypatch.setattr(wall, "spread_image", spread)
    run = subprocess.run
    output = []

    def icat(args, **kwargs):
        # Supply real cell dimensions without requiring an interactive terminal.
        result = run(
            [*args, f"--use-window-size=60,24,480,{24 * cell_height}"],
            **kwargs, stdout=subprocess.PIPE,
        )
        output.append(result.stdout.decode())
        return result

    monkeypatch.setattr(cli.subprocess, "run", icat)
    cli.main(["draw", str(n)])
    width, height = dimensions[0]
    rows = [line for line in output[0].split("\n") if "\U0010eeee" in line]
    assert len(rows) == math.ceil(height / cell_height)
    assert all(line.count("\U0010eeee") == math.ceil(width / 8) for line in rows)
    assert output[0].endswith("\n")  # The cursor is below the last placeholder row.


def test_older_kitty_launcher(rendering, monkeypatch):
    monkeypatch.setenv("TERM", "xterm-kitty")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    cli.main(["draw"])
    assert rendering[-1][0][:3] == ["kitty", "+kitten", "icat"]


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.CalledProcessError(1, "icat")])
def test_unavailable_kitty_renderer_keeps_caption(rendering, monkeypatch, capsys, error):
    monkeypatch.setenv("TERM", "xterm-kitty")
    run = cli.subprocess.run

    def fail_icat(args, **kwargs):
        if args[0] != "magick":
            raise error
        return run(args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", fail_icat)
    cli.main(["draw", "--card", "the tower"])
    assert "XVI The Tower" in capsys.readouterr().out


def test_missing_art_keeps_caption(rendering, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TERM", "xterm-kitty")
    monkeypatch.setattr(cli, "_art_dir", lambda: tmp_path / "missing")
    cli.main(["draw", "--card", "the tower"])
    assert "XVI The Tower" in capsys.readouterr().out
    assert rendering == []

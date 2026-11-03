"""Markers to Live locators: synthetic MIDI files and a minimal made-up Live set, so CI needs no Live."""
import gzip
import os

import pytest

from midi_tempo import ableton as A
from tests.test_core import PPQ, smf, tempo, text, timesig, track

# The parts of a Live set this module reads and writes, shaped like Live 12's own default set.
SET = """<?xml version="1.0" encoding="UTF-8"?>
<Ableton MajorVersion="5" MinorVersion="12.0_12117" SchemaChangeCount="10" Creator="Ableton Live 12.1d1" Revision="x">
\t<LiveSet>
\t\t<NextPointeeId Value="22182" />
\t\t<Tracks>
\t\t\t<AudioTrack Id="1"><Name><EffectiveName Value="Stems" /></Name>
\t\t\t\t<ArrangerAutomation><Events><AudioClip Id="0" Time="0"><CurrentEnd Value="{clip_end}" /></AudioClip></Events></ArrangerAutomation>
\t\t\t</AudioTrack>
\t\t</Tracks>
\t\t<MainTrack SelectedToolPanel="7">
\t\t\t<AutomationEnvelopes>
\t\t\t\t<Envelopes>
\t\t\t\t\t<AutomationEnvelope Id="0"><EnvelopeTarget><PointeeId Value="10" /></EnvelopeTarget><Automation><Events><EnumEvent Id="0" Time="-63072000" Value="201" /></Events></Automation></AutomationEnvelope>
\t\t\t\t\t<AutomationEnvelope Id="1"><EnvelopeTarget><PointeeId Value="8" /></EnvelopeTarget><Automation><Events>{tempo_events}</Events></Automation></AutomationEnvelope>
\t\t\t\t</Envelopes>
\t\t\t</AutomationEnvelopes>
\t\t\t<DeviceChain><Mixer>
\t\t\t\t<Tempo><LomId Value="0" /><Manual Value="{bpm}" /><AutomationTarget Id="8"><LockEnvelope Value="0" /></AutomationTarget></Tempo>
\t\t\t\t<TimeSignature><LomId Value="0" /><Manual Value="201" /><AutomationTarget Id="10"><LockEnvelope Value="0" /></AutomationTarget></TimeSignature>
\t\t\t</Mixer></DeviceChain>
\t\t</MainTrack>
\t\t<Locators>
\t\t\t{locators}
\t\t</Locators>
\t</LiveSet>
</Ableton>
"""


def live_set(path, locators=(), bpm=120, tempo_events=None, clip_end=0, extra=""):
    locs = "".join(
        f'<Locator Id="{i + 3}"><LomId Value="0" /><Time Value="{t}" /><Name Value="{nm}" />'
        f'<Annotation Value="{note}" /><IsSongStart Value="false" /></Locator>'
        for i, (t, nm, note) in enumerate(locators))
    ev = tempo_events or f'<FloatEvent Id="0" Time="-63072000" Value="{bpm}" />'
    xml = SET.format(locators=f"<Locators>{locs}</Locators>" if locs else "<Locators />", bpm=bpm,
                     tempo_events=ev, clip_end=clip_end).replace("</Tracks>", extra + "</Tracks>")
    with open(path, "wb") as f:
        f.write(gzip.compress(xml.encode()))
    return str(path)


def marker(s):
    return text(s, kind=6)


def midi(path, events, bpm=120, sig=(4, 2)):
    tr = [(0, tempo(bpm)), (0, timesig(*sig))] + events
    path.write_bytes(smf([track(tr)]))
    return str(path)


@pytest.fixture
def template(tmp_path):
    return live_set(tmp_path / "Default.als")


def test_markers_become_ableset_locators(tmp_path, template):
    m = midi(tmp_path / "Fresh Air - midi track.mid",
             [(0, marker("Intro")), (8 * 4 * PPQ, marker("Verse 1")), (16 * 4 * PPQ, marker("Stop")),
              (24 * 4 * PPQ, marker("end")), (24 * 4 * PPQ, bytes([0x90, 60, 90])),
              (25 * 4 * PPQ, bytes([0x80, 60, 0]))])
    p = A.plan_song(m)
    assert p.name == "Fresh Air"
    names = [(l.beat, l.name) for l in p.locators]
    # A band's "Stop" stays a section; only AbleSet's own capitals are commands
    assert names == [(0, "Fresh Air"), (0, "> Intro"), (32, "> Verse 1"), (64, "> Stop"), (96, "SONG END")]
    r = A.new_set([p], str(tmp_path / "Out Project" / "Out.als"), template)
    got = [(l.beat, l.name) for l in A.LiveSet.load(r.path).locators()]
    # the note rings a bar past SONG END: a note locator AbleSet ignores marks where it stops
    assert got == names + [(100, "* Fresh Air: the music ends here")]
    assert "music goes on 1 bar past" in " ".join(p.said)


def test_tempo_steps_and_time_signatures_land_exactly(tmp_path, template):
    ev = [(0, marker("A")), (4 * PPQ * 4, tempo(90)), (4 * PPQ * 4, marker("B")),
          (8 * PPQ * 4, timesig(3, 2)), (8 * PPQ * 4, marker("C")), (8 * PPQ * 4 + 9 * PPQ, marker("D")),
          (8 * PPQ * 4 + 9 * PPQ, tempo(140))]
    p = A.plan_song(midi(tmp_path / "s.mid", ev))
    r = A.new_set([p], str(tmp_path / "s.als"), template)
    ls = A.LiveSet.load(r.path)
    tm = ls.tempo()
    for loc in p.locators:
        assert abs(tm.seconds(loc.beat) - loc.seconds) < 1e-6
    assert [(b, n_, d) for b, n_, d in ls.sigs()] == [(0.0, 4, 4), (32.0, 3, 4)]
    d = [l for l in p.locators if l.name == "> D"][0]
    assert A.bar_number(p.sigs, d.beat) == (12, 1)  # 4/4 for 8 bars, then 3/4: beat 9 into it is bar 12


def test_bare_section_is_never_written(tmp_path, template):
    """AbleSet 3.1.9 hangs Live loading a set with a ">" locator; an unnamed marker gets a name."""
    p = A.plan_song(midi(tmp_path / "s.mid", [(0, marker("Intro")), (4 * PPQ * 4, marker("")),
                                               (8 * PPQ * 4, marker("> +LOOP"))]))
    names = [l.name for l in p.locators]
    assert "> Section 2" in names and "> Section 3 +LOOP" in names
    assert not any(A.has_no_name(nm) for nm in names)


def test_typed_markers_any_number_at_bar_and_beat(tmp_path, template):
    lines = "\n".join(f"{bar} Part {bar}" for bar in range(1, 2001)) + "\n2001 End"
    typed = A.parse_markers(lines + "\n33.3 Pickup\nbar 40 beat 2.5 Hit\n49.3.50 Fill")
    p = A.plan_song(None, "Long Song", typed=typed, tempo=100, signature=(6, 8))
    sections = p.sections
    assert len(sections) == 2003
    hit = [l for l in sections if l.name == "> Hit"][0]
    assert hit.beat == pytest.approx(39 * 3 + 1.5 * 0.5)  # 6/8: a bar is 3 quarter notes, an eighth is half
    fill = [l for l in sections if l.name == "> Fill"][0]
    assert fill.beat == pytest.approx(48 * 3 + 2.5 * 0.5)
    r = A.new_set([p], str(tmp_path / "long.als"), template)
    assert len(A.LiveSet.load(r.path).locators()) == 2005


def test_typed_marker_out_of_its_bar_is_refused():
    with pytest.raises(A.SetError, match="no beat 5"):
        A.plan_song(None, "S", typed=A.parse_markers("3.5 Too far"), signature=(4, 4))
    with pytest.raises(A.SetError, match="line 2"):
        A.parse_markers("1 Fine\nChorus at 9")


def test_typed_marker_replaces_the_files_at_the_same_place(tmp_path):
    p = A.plan_song(midi(tmp_path / "s.mid", [(4 * PPQ * 4, marker("Verse"))]),
                    typed=A.parse_markers("5 Verse 1\n9 Chorus"))
    assert [l.name for l in p.sections] == ["> Verse 1", "> Chorus"]
    assert any("takes the place" in x for x in p.said)


def test_after_the_last_song_keeps_everything_and_starts_on_a_bar(tmp_path):
    s = live_set(tmp_path / "Show.als", [(0, "Opener", "keys in A"), (40, "SONG END", "")], clip_end=41.5)
    p = A.plan_song(midi(tmp_path / "Second.mid", [(0, marker("Intro")), (8 * PPQ, tempo(80)),
                                                    (8 * PPQ, marker("Verse")), (12 * PPQ, bytes([0x90, 60, 90])),
                                                    (14 * PPQ, bytes([0x80, 60, 0]))]))
    r = A.add_to_set(s, [p])
    ls = A.LiveSet.load(r.path)
    locs = [(l.beat, l.name) for l in ls.locators()]
    assert (0, "Opener") in locs and (40, "SONG END") in locs
    assert (44, "Second") in locs and (52, "> Verse") in locs  # next bar line after the clip's end
    kept = [l for l in ls.locators() if l.name == "Opener"][0]
    assert 'Annotation Value="keys in A"' in kept.xml  # untouched, info text and all
    tm = ls.tempo()
    assert tm.seconds(52) - tm.seconds(44) == pytest.approx(4.0)  # 8 beats at 120
    assert tm.seconds(56) - tm.seconds(52) == pytest.approx(3.0)  # then 4 at 80
    assert os.path.basename(r.path) == "Show (markers added).als"
    original = gzip.decompress(open(s, "rb").read())
    assert b"Second" not in original  # the set itself is never changed


def test_onto_a_song_replaces_sections_and_keeps_the_tempo(tmp_path):
    s = live_set(tmp_path / "Show.als", [(0, "Song A [blue]", ""), (16, "> Old", ""), (24, "* note", ""),
                                         (64, "Song B", "")], bpm=100)
    p = A.plan_song(midi(tmp_path / "Song A.mid", [(4 * PPQ * 4, marker("New"))], bpm=100))
    r = A.add_to_set(s, [p], onto="Song A")
    names = [(l.beat, l.name) for l in A.LiveSet.load(r.path).locators()]
    assert (16, "> Old") not in names and (16, "> New") in names
    assert (24, "* note") in names and (0, "Song A [blue]") in names
    assert not any(nm.startswith("SONG END") for _, nm in names)  # Song B already ends it


def test_onto_with_a_different_tempo_asks_how_to_line_up(tmp_path):
    s = live_set(tmp_path / "Show.als", [(0, "Song A", "")], bpm=120)
    m = midi(tmp_path / "a.mid", [(8 * PPQ * 4, marker("Late"))], bpm=60)
    with pytest.raises(A.NeedsLineUp):
        A.add_to_set(s, [A.plan_song(m)], onto="Song A")
    by_bar = A.add_to_set(s, [A.plan_song(m)], onto="Song A", line_up="bars", out=str(tmp_path / "b.als"))
    by_time = A.add_to_set(s, [A.plan_song(m)], onto="Song A", line_up="time", out=str(tmp_path / "t.als"))
    beat = lambda r: [l.beat for l in A.LiveSet.load(r.path).locators() if l.name == "> Late"][0]  # noqa: E731
    assert beat(by_bar) == pytest.approx(32)
    assert beat(by_time) == pytest.approx(64)  # 32 s at 60 BPM is 64 beats at 120


def test_typed_markers_onto_a_song_count_bars_from_its_start(tmp_path):
    s = live_set(tmp_path / "Show.als", [(0, "Opener", ""), (40, "Song B", "")])
    p = A.plan_song(None, "Song B", typed=A.parse_markers("2 Verse\n3.3 Hit"))
    r = A.add_to_set(s, [p], onto="Song B")
    names = {l.name: l.beat for l in A.LiveSet.load(r.path).locators()}
    assert names["> Verse"] == 44 and names["> Hit"] == 50


def test_update_the_set_keeps_a_backup(tmp_path, template):
    s = live_set(tmp_path / "Show.als", [(0, "Opener", "")])
    before = open(s, "rb").read()
    p = A.plan_song(None, "Two", typed=A.parse_markers("1 Intro\n5 End"))
    r = A.add_to_set(s, [p], in_place=True)
    assert r.path == s
    assert open(tmp_path / "Show (before markers).als", "rb").read() == before


def test_songs_in_a_set_read_like_ableset(tmp_path):
    s = live_set(tmp_path / "Show.als", [(0, ". Opener {Capo 2} #rock", ""), (8, "> Verse", ""),
                                         (16, "SONG END", ""), (20, "* idea", ""), (24, "Closer [nosong]", "")])
    songs = A.set_songs(A.LiveSet.load(s))
    assert [A.song_title(x.name) for x, _ in songs] == ["Opener", "Closer"]


def test_names_are_escaped(tmp_path, template):
    p = A.plan_song(None, 'Rock & "Roll" <Live>', typed=A.parse_markers("1 A & B\n3 End"))
    r = A.new_set([p], str(tmp_path / "x.als"), template)
    assert [l.name for l in A.LiveSet.load(r.path).locators()] == ['Rock & "Roll" <Live>', "> A & B", "SONG END"]


def test_new_set_never_overwrites(tmp_path, template):
    p = A.plan_song(None, "S", typed=A.parse_markers("1 A"))
    out = str(tmp_path / "S.als")
    A.new_set([p], out, template)
    with pytest.raises(A.SetError, match="already exists"):
        A.new_set([p], out, template)


def test_markers_as_text_round_trip():
    ms = [A.TypedMarker(17, 1, "Verse"), A.TypedMarker(33, 3, "Pickup"), A.TypedMarker(214, 2.008333, ""),
          A.TypedMarker(9, 1, "2nd verse"), A.TypedMarker(5, 2.5, "> Chorus +LOOP"), A.TypedMarker(7, 1, "3.5 reprise")]
    assert A.parse_markers("\n".join(A.marker_line(m) for m in ms)) == ms


def test_curved_tempo_points_are_kept_when_a_song_is_added(tmp_path):
    curved = ('<FloatEvent Id="0" Time="-63072000" Value="120" />'
              '<FloatEvent Id="1" Time="8" Value="120" CurveControl1X="0.5" CurveControl1Y="0.2" '
              'CurveControl2X="0.5" CurveControl2Y="0.8" /><FloatEvent Id="2" Time="16" Value="100" />')
    s = live_set(tmp_path / "Show.als", [(0, "Opener", "")], tempo_events=curved, clip_end=16)
    r = A.add_to_set(s, [A.plan_song(None, "Two", typed=A.parse_markers("1 Intro\n3 End"))])
    text = A.LiveSet.load(r.path).text
    assert 'CurveControl1X="0.5" CurveControl1Y="0.2"' in text
    ids = [int(x) for x in __import__("re").findall(r'<FloatEvent Id="(\d+)"', text)]
    assert len(ids) == len(set(ids))


def test_windows_live_folders_sort_newest_first_betas_last(monkeypatch):
    paths = [r"C:\ProgramData\Ableton\Live 11 Suite\Resources\Builtin\Templates\DefaultLiveSet.als",
             r"C:\ProgramData\Ableton\Live 12 Beta\Resources\Builtin\Templates\DefaultLiveSet.als",
             r"C:\ProgramData\Ableton\Live 12 Suite\Resources\Builtin\Templates\DefaultLiveSet.als"]
    monkeypatch.setattr(A.sys, "platform", "win32")
    monkeypatch.setattr(A.glob, "glob", lambda pat: paths)
    assert [name for name, _ in A.live_templates()] == ["Live 12 Suite", "Live 11 Suite", "Live 12 Beta"]


def test_end_marker_at_the_next_song_is_not_reported_lost(tmp_path):
    s = live_set(tmp_path / "Show.als", [(0, "Song A", ""), (32, "Song B", "")])
    p = A.plan_song(midi(tmp_path / "a.mid", [(4 * PPQ * 4, marker("Verse")), (8 * PPQ * 4, marker("End"))]))
    r = A.add_to_set(s, [p], onto="Song A")
    assert not any("left out" in x for x in r.said)


def test_a_song_added_later_starts_after_a_ringing_tail(tmp_path, template):
    m = midi(tmp_path / "Ring.mid", [(0, marker("Intro")), (4 * PPQ * 4, marker("End")),
                                     (4 * PPQ * 4, bytes([0x90, 60, 90])), (8 * PPQ * 4, bytes([0x80, 60, 0]))])
    first = A.new_set([A.plan_song(m)], str(tmp_path / "Set.als"), template)
    r = A.add_to_set(first.path, [A.plan_song(None, "Next", typed=A.parse_markers("1 Intro\n3 End"))])
    next_start = [l.beat for l in A.LiveSet.load(r.path).locators() if l.name == "Next"][0]
    assert next_start >= 32  # the note rings to bar 9, past SONG END at bar 5


def test_a_beat_past_a_short_bar_is_refused():
    with pytest.raises(A.SetError, match="only 2 beats long"):
        A.plan_song(None, "S", typed=A.parse_markers("2.4 Too late"), set_sigs=[(0.0, 4, 4), (6.0, 3, 4)])


def test_song_end_ignores_a_late_end_of_track(tmp_path):
    m = tmp_path / "late.mid"
    tr = track([(0, tempo(120)), (0, timesig(4, 2)), (0, marker("Intro")), (0, bytes([0x90, 60, 90])),
                (PPQ, bytes([0x80, 60, 0])), (40 * PPQ * 4, text("trailing", 1))])
    m.write_bytes(smf([tr]))
    p = A.plan_song(str(m))
    assert [l.beat for l in p.locators if l.name == "SONG END"] == [4.0]


def test_end_can_share_the_last_beat_with_another_marker(tmp_path):
    p = A.plan_song(None, "S", typed=A.parse_markers("5 End\n5 Outro\n1 Intro"))
    assert [l.name for l in p.locators if l.beat == 16] == ["> Outro", "SONG END"]


def test_awkward_names_survive_edit_as_text():
    ms = [A.TypedMarker(9, 1, "Beat 2 drop"), A.TypedMarker(10, 1, ": Chorus"), A.TypedMarker(11, 3, "- tag"),
          A.TypedMarker(12, 1, ".hidden")]
    assert A.parse_markers("\n".join(A.marker_line(m) for m in ms)) == ms

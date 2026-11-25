"""Waggle Home's memory graph (home.graph, home.note) and the HUD page file."""
import json

from waggle import home, vault

from test_home import call, srv  # noqa: F401  (the server fixture)


def test_graph_has_one_node_per_note_and_links_from_wikilinks(settings):
    home._graph_cache.update(at=0.0, data=None)
    vault.write("Projects/Robot.md", "# Robot\n[[Shooter]] and [[Shooter]] again, [[Nobody]]\n", settings)
    vault.write("Projects/Shooter.md", "# Shooter\nsee [[Robot|the robot]]\n", settings)
    vault.write("Ideas/Loose.md", "# Loose\n", settings)
    g = home.graph(settings, now=1000.0)
    names = {n["name"]: n for n in g["nodes"]}
    assert {"Robot", "Shooter", "Loose"} <= set(names)
    assert len(g["links"]) == 1 and names["Robot"]["degree"] == 1 and names["Loose"]["degree"] == 0
    assert home.graph(settings, now=1001.0) is g          # cached
    note = home.note(names["Robot"]["id"], settings)
    assert note["name"] == "Robot" and "[[Shooter]]" in note["text"]
    assert "error" in home.note("../../etc/passwd", settings) and "error" in home.note("Nope.md", settings)


def test_groups_are_top_folders_and_two_levels_inside_containers(settings, tmp_path):
    home._graph_cache.update(at=0.0, data=None)
    root = vault.root(settings)
    for rel in ("01 - Areas/Identity/Me.md", "01 - Areas/Waggle/Projects/X.md", "00 - Current Projects/P.md", "Top.md"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# n\n", encoding="utf-8")
    g = home.graph(settings, now=5.0)
    groups = {n["name"]: g["groups"][n["group"]] for n in g["nodes"]}
    assert groups["Me"] == "01 - Areas/Identity" and groups["X"] == "01 - Areas/Waggle"
    assert groups["P"] == "00 - Current Projects" and groups["Top"] == "(top)"


def test_graph_and_note_are_served(srv, settings):  # noqa: F811
    home._graph_cache.update(at=0.0, data=None)
    code, _, body = call(srv + "/api/graph")
    assert code == 200 and "nodes" in json.loads(body)
    code, _, body = call(srv + "/api/note?path=..%2Fsecret.md")
    assert code == 200 and "error" in json.loads(body)


def test_the_page_is_the_hud_with_the_memory_view():
    page = home.page()
    assert home.TOKEN in page and "__TOKEN__" not in page
    for part in ('id="reactor"', 'id="graphview"', "/api/graph", "/api/note?path=", 'id="tab-mem"'):
        assert part in page, part

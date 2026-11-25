"""False asks found by the 2026-11-25 real-project tests: read-only commands the guard held as changing safety files."""
from pathlib import Path

from waggle import guard

HERE = str(Path(guard.__file__).resolve().parents[1])


def test_a_sed_pattern_with_stars_names_no_file():
    # the build job's environment check: gh auth status piped through sed to hide the token
    cmd = ('cd "' + HERE + '"; ls; GH=$(command -v gh || echo x); "$GH" auth status 2>&1 | sed \'s/Token: .*/Token: ***/\'; '
           "git config --global user.email; node --version; python --version")
    assert guard.shell_hit(cmd, cwd="C:/x", shell="bash") is None


def test_printing_a_property_of_a_recursive_listing_is_a_read():
    cmd = ('Get-ChildItem -Path "' + HERE + '" -Recurse -Depth 6 -Filter \'plan-shortlist.md\' '
           "-ErrorAction SilentlyContinue | ForEach-Object FullName")
    assert guard.shell_hit(cmd, cwd="C:/x", shell="powershell") is None
    assert guard.shell_hit('Get-ChildItem "' + HERE + '" -Recurse | % -MemberName Name', cwd="C:/x",
                           shell="powershell") is None


def test_calling_a_method_through_foreach_still_asks():
    for tail in ("% Delete", "ForEach-Object Delete", "ForEach-Object { Remove-Item $_.FullName }"):
        assert guard.shell_hit(f'Get-ChildItem "{HERE}/waggle" -Recurse | {tail}', cwd="C:/x", shell="powershell")


def test_an_in_place_sed_on_a_vault_note_is_not_a_command_runner():
    # the college tracker: a substitution whose text has words like "schools section" was read as s///e
    cmd = ("sed -i 's/^Dates below are the usual yearly windows.*$/The Target schools section was checked online. "
           "Everything below it is from general knowledge: confirm it./' "
           "\"/c/Users/x/PersonalIntelligenceVault/12 - Waggle/Goals/College Application Tracker.md\"")
    assert guard.shell_hit(cmd, cwd="C:/x", shell="bash") is None
    for ok in (["-i", "s/a/b/g", "f.md"], ["-i.bak", "-e", "1i # title", "f.md"], ["s/a/b/w out.txt", "f"],
               ["--in-place=.orig", "/x/d", "f"]):
        assert not guard._runs_commands("sed", ok), ok


def test_sed_that_runs_commands_still_counts():
    for bad in (["s/a/b/e", "f"], ["1e rm -rf /", "f"], ["-e", "s/x/y/;e date", "f"], ["-f", "prog.sed", "f"],
                ["--file=prog.sed", "f"], ["-n", "s/x/y/ep", "f"]):
        assert guard._runs_commands("sed", bad), bad
    assert guard.shell_hit(f"sed -i 's/a/b/e' \"{HERE}/waggle/policy.py\"", cwd="C:/x", shell="bash")


def test_wildcards_still_count_for_writers():
    assert guard.shell_hit("rm -rf ***/", cwd=HERE, shell="bash")
    assert guard.shell_hit("sed -i s/a/b/ ***/policy.py", cwd=HERE, shell="bash")

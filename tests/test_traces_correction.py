import pytest

from waggle import traces


@pytest.mark.parametrize("msg", ["no thanks", "No thanks!", "no problem", "No worries!", "no need", "no rush",
                                 "no, that's fine", "No, that's great", "no, that's perfect", "no, thank you"])
def test_polite_no_is_not_a_correction(msg):
    assert not traces.CORRECTION.search(msg)


@pytest.mark.parametrize("msg", ["no, that is wrong", "No", "nope, wrong date", "no that's not right",
                                 "no, my team meets on Tuesday"])
def test_real_corrections_still_count(msg):
    assert traces.CORRECTION.search(msg)

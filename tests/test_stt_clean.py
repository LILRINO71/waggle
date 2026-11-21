"""Clean transcripts: what a person does not mean to say is removed, and nothing else is touched."""
import pytest

from waggle import stt

CHANGES = [
    # filler sounds
    ("Um, can you send me the report?", "Can you send me the report?"),
    ("So, um, what time is the meet?", "So, what time is the meet?"),
    ("I want, uh, the red one.", "I want, the red one."),
    ("Uhh I think it's done", "I think it's done"),
    ("Er, remind me tomorrow", "Remind me tomorrow"),
    ("Hmm. Let me think about that.", "Let me think about that."),
    ("Ahh, I see what you mean.", "I see what you mean."),
    ("Open the file, erm, and read the first line.", "Open the file, and read the first line."),
    ("Ummm okay so send it", "Okay so send it"),
    # filler phrases at clause edges
    ("You know, I think we should wait.", "I think we should wait."),
    ("I like, you know, pizza.", "I like, pizza."),
    ("Send it to Bob, you know.", "Send it to Bob."),
    ("I mean, can you send it today?", "Can you send it today?"),
    ("So yeah, what is next on the list?", "What is next on the list?"),
    ("It should work, so yeah.", "It should work."),
    # immediate repeats
    ("Remind me to to call Sam.", "Remind me to call Sam."),
    ("The the build is red.", "The build is red."),
    ("I I want the the red one.", "I want the red one."),
    ("I, I want to go.", "I want to go."),
    ("Check the, the build and and the logs.", "Check the build and the logs."),
    # an abandoned start and its restart
    ("Can you, could you send me the file?", "Could you send me the file?"),
    ("Can you can you send it", "Can you send it"),
    ("I want to, I need to leave early.", "I need to leave early."),
    ("Set a reminder, set a reminder for six PM.", "Set a reminder for six PM."),
    ("wh- what time is it", "what time is it"),
    ("I- I want the file", "I want the file"),
    # several at once
    ("Um, I mean, can you check the, the build?", "Can you check the build?"),
    ("Uh... so yeah, remind me at 3, um, tomorrow.", "Remind me at 3, tomorrow."),
    ("Remind me at five, um, no, six.", "Remind me at five, no, six."),
]

UNCHANGED = [
    "I like this song.",  # "like" as a verb
    "Do you like pasta?",
    "I mean it.",  # "I mean" followed by no pause
    "She said I mean it, you know what I mean?",
    "As you know, the meeting is at three.",
    "Do you know, sir, what time it is?",
    'Play "Um Bongo" on my phone.',  # a quoted title
    "Play “Uh Oh” by someone.",
    "The ER is busy tonight.",
    "Um Kalthoum sang that.",  # a capitalised sound straight before a name
    "Mm-hmm, uh-huh, yes.",  # affirmations
    "Uh-oh, that broke.",
    "Use a 10 mm bolt.",  # "mm" is millimetres
    "I need a 12 volt 7 ah battery.",  # "ah" is amp-hours
    "Coat it 10 um thick.",  # "um" as micrometres
    "Send it at 3, I mean, 4.",
    "That that is what I meant.",  # legitimate repeats
    "He had had enough.",
    "No no no, that is wrong.",
    "That is very very good.",
    "Thank you. Thank you.",
    "Wait, wait, wait.",
    "It was 10 10 on the dot.",  # numbers are never touched
    "Take two two-hour breaks.",
    "I like pizza, I like pasta, I like soup.",  # a list is not a restart
    "Should I, or should you, go?",
    "Hmm.",  # all that is left would be nothing
    "um um um",
    "",
    "   ",
]


@pytest.mark.parametrize("raw,expected", CHANGES)
def test_what_a_person_does_not_mean_to_say_is_removed(raw, expected):
    assert stt.clean_transcript(raw) == expected


@pytest.mark.parametrize("raw", UNCHANGED)
def test_what_could_be_meant_is_left_alone(raw):
    assert stt.clean_transcript(raw) == raw


def test_there_are_at_least_twenty_examples_each_way():
    assert len(CHANGES) >= 20 and len(UNCHANGED) >= 20


def test_cleaning_twice_changes_nothing_more():
    for raw, _ in CHANGES:
        once = stt.clean_transcript(raw)
        assert stt.clean_transcript(once) == once


def test_a_quoted_span_keeps_its_fillers_while_the_rest_is_cleaned():
    assert stt.clean_transcript('Um, play "Um Bongo", you know, please.') == 'Play "Um Bongo", please.'


def test_lines_stay_lines():
    assert stt.clean_transcript("Look at this\n\nUm, the the file") == "Look at this\n\nThe file"


def test_the_result_is_always_the_original_with_words_removed():
    import random
    words = ("um uh the the I I can you could send 5 6 mean know so yeah like it , . ? mm hmm er "
             "Bob \"Um\" a a to to").split()
    rng = random.Random(7)
    for _ in range(400):
        raw = " ".join(rng.choice(words) for _ in range(rng.randint(1, 14)))
        out = stt.clean_transcript(raw)
        theirs, pos = stt._words(raw), 0
        for w in stt._words(out):  # a subsequence of the original words, and the numbers survive in order
            pos = theirs.index(w, pos) + 1
        assert [w for w in stt._words(out) if w.isdigit()] == [w for w in theirs if w.isdigit()]
        assert stt._words(out) or not stt._words(raw)

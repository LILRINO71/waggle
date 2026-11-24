"""The 30 turns both paths run (Step 2, decision doc 1.1). Turn 1 sets a code word; turn 16 (after the kill and
resume between turns 15 and 16) asks for it back. Turns with tools use Bash, Glob and Read in the trial folder."""
CODE_WORD = "PELICAN-7"
SYSTEM = "This is a test session. Answer in one short sentence. Use a tool only when the message asks for one."
PROMPTS = (
    [f"Remember the code word {CODE_WORD}. Reply with just: OK"]
    + ["What is 17 times 23?", "Name the capital of Japan.", "Use Bash to run: echo trial-3 and tell me the output.",
       "Use Glob to list the .txt files in the current folder.", "Read notes.txt and tell me its second line.",
       "Is 97 a prime number?", "Use Bash to run: echo trial-8 and tell me the output.", "Spell 'robot' backwards.",
       "What is the square root of 144?", "Use Bash to run: python --version and tell me the version.",
       "How many days are in a leap year?", "Read notes.txt and tell me how many lines it has.",
       "What is 2 to the power of 10?", "Use Glob to count the files in the current folder."]
    + ["What was the code word I gave you at the start?"]
    + ["What is 45 divided by 9?", "Use Bash to run: echo trial-18 and tell me the output.", "Name a primary color.",
       "Read notes.txt and tell me its first line.", "What is 13 plus 29?", "Use Bash to run: echo trial-22.",
       "What is the boiling point of water in Celsius?", "Use Glob to list the .md files in the current folder.",
       "What is 9 squared?", "Use Bash to run: echo trial-26.", "Name the largest planet.",
       "Read notes.txt and tell me its last line.", "What is 100 minus 37?", "Say goodbye in one word."]
)
NOTES = "alpha line\nbravo line\ncharlie line\n"
assert len(PROMPTS) == 30

import json, sys, time, urllib.request, re
Q = [
 ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much is the ball in cents? Answer with the number only.", r"\b5\b"),
 ("If it takes 5 machines 5 minutes to make 5 widgets, how many minutes would 100 machines take to make 100 widgets? Number only.", r"\b5\b"),
 ("I leave at 3:40 PM and the drive takes 2 hours 35 minutes. What time do I arrive? Answer like 6:15 PM.", r"6:15\s*PM"),
 ("A robot wheel has a 96 mm diameter. How many centimeters does the robot travel in 10 full turns? Round to the nearest whole number. Number only.", r"\b302\b"),
 ("Today is Friday November 20, 2026. What is the date next Tuesday? Answer like 2026-11-24.", r"2026-11-24"),
 ("Sort these from smallest to largest and answer as a comma list: 0.3, 1/4, 2/7, 0.28", r"1/4,\s*0\.28,\s*2/7,\s*0\.3"),
 ("Alice is older than Bob. Carol is younger than Bob. Dan is older than Alice. Who is the second youngest? One name.", r"\bBob\b"),
 ("How many times does the letter r appear in the word strawberry? Number only.", r"\b3\b"),
]
def call(model, q):
    body = {"model": model, "messages": [{"role": "user", "content": q}], "stream": False, "think": False,
            "options": {"temperature": 0, "num_ctx": 4096}, "keep_alive": "5m"}
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=600) as r: d = json.load(r)
    return d["message"]["content"].strip(), time.time() - t
for m in sys.argv[1:]:
    call(m, "hi")
    ok, ts = 0, []
    for q, pat in Q:
        a, s = call(m, q); ts.append(s); hit = bool(re.search(pat, a, re.I)); ok += hit
        if not hit: print(f"  {m} MISS: {q[:50]} -> {a[:80]!r}")
    print(m, f"{ok}/{len(Q)}", "avg s", round(sum(ts)/len(ts), 1), "max s", round(max(ts), 1), flush=True)
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate", data=json.dumps({"model": m, "keep_alive": 0}).encode(), headers={"Content-Type": "application/json"}), timeout=60).read()
    time.sleep(5)

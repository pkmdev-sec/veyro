import concurrent.futures as cf
import json
import re
import sys
import urllib.request

CASES = [
    (" Hello, World! ", "hello-world"),
    ("A___B---C", "a-b-c"),
    ("...", ""),
    ("", ""),
    ("abc123", "abc123"),
    ("a\nb\tc", "a-b-c"),
    ("CAF\u00c9", "caf"),
    ("a  -- !! b", "a-b"),
]


def ask(model, prompt, temp=0.0, seed=0):
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": temp, "seed": seed, "num_predict": 2048},
        }
    ).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def extract(reply):
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    blocks = re.findall(r"```(?:python)?\s*(.*?)```", reply, re.S)
    defining = [b for b in blocks if re.search(r"def\s+slug\s*\(", b)]
    code = defining[-1] if defining else (blocks[-1] if blocks else reply)
    return code


def score(code):
    ns = {}
    try:
        exec(code, ns)
        slug = ns["slug"]
    except Exception as e:
        return None, f"exec-error: {type(e).__name__}", []
    passed, fails = 0, []
    for t, e in CASES:
        try:
            got = slug(t)
        except Exception as ex:
            got = f"<{type(ex).__name__}>"
        if got == e:
            passed += 1
        else:
            fails.append((t, got, e))
    return passed, None, fails


def run(name, model, prompt, seed):
    reply = ask(model, prompt, seed=seed)
    code = extract(reply)
    p, err, fails = score(code)
    uses_W = bool(re.search(r"\\\\W", code))
    return {
        "variant": name,
        "model": model,
        "seed": seed,
        "passed": p,
        "error": err,
        "uses_\\\\W": uses_W,
        "unicode_ok": all(t != "CAF\u00c9" for t, _, _ in fails) if p is not None else False,
        "fails": [(t, g, e) for t, g, e in fails],
        "code": code.strip(),
    }


if __name__ == "__main__":
    spec = json.load(open(sys.argv[1]))
    jobs = [
        (v["name"], spec["model"], v["prompt"], s) for v in spec["variants"] for s in spec["seeds"]
    ]
    out = []
    with cf.ThreadPoolExecutor(max_workers=spec.get("workers", 2)) as ex:
        futs = {ex.submit(run, *j): j for j in jobs}
        for f in cf.as_completed(futs):
            j = futs[f]
            try:
                out.append(f.result())
            except Exception as e:
                out.append({"variant": j[0], "seed": j[3], "error": repr(e), "passed": None})
    json.dump(out, open(spec["output"], "w"), indent=1)
    print("wrote", spec["output"], len(out), "runs")

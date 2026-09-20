# Source transcript: "Building a Harness with Jev"

Durable copy of the third specification source used by Gate G0. It replaces the
earlier `/tmp` evidence files, which do not survive a reboot.

| Field | Value |
| --- | --- |
| Title | Building a Harness with Jev |
| Author | Sydney Runkle (`@sydneyrunkle`, product + open source @LangChain) |
| Article URL | <https://x.com/i/article/2100744524951932928> |
| Linking tweet | <https://x.com/sydneyrunkle/status/2100754364545761643> |
| Published | 2026-09-18T01:10:45.000Z |
| Retrieved from | `https://api.fxtwitter.com/sydneyrunkle/status/2100754364545761643` |
| Machine-readable copy | [building-a-harness-with-jev.json](building-a-harness-with-jev.json) |

## What this article is, and is not

The article is a **LangChain** post about **TypeSafe AI's Jev**, a "System One"
classification model, and the `langchain-typesafe` integration. Its three named
software components are `TypeSafeClassifier`, `ModelRouterMiddleware`, and
`AutoModeMiddleware`.

It is **not** a specification for a Jev or "Sydney" coding-agent supervisor.
"Sydney" is the author's first name. Earlier Veyro notes that called this "the
Sydney article" or treated it as a supervisor spec were wrong. The recovered body
contains no occurrence of "supervisor", "checkpoint", "judge", "rubric", or
"stop condition", and it does not describe scoring a coding agent's output to
decide whether that agent should continue.

Veyro's supervisor, checkpoint questions, and stopping logic therefore do not rest
on this article. They are Veyro's own design. See
[native-judge.md](../native-judge.md) for what each source did and did not
contribute.

## What Veyro legitimately takes from it

Confirmed present in the verbatim text below:

- The three typed question kinds, quoted exactly: "Choice: Pick from a set of
  options. Returns a probability for each option and an overall confidence
  score.", "Score: Rate an input against ordered levels, such as low, medium, and
  high. Returns a continuous score, the underlying distribution, and a confidence
  value.", and "Noul: Answer a yes-or-no question. Returns the probability that a
  statement is true."
- One shared state per request with many questions: "you can ask multiple
  questions about the same state in one request."
- Parallel evaluation: "System One models evaluate every question in a request in
  parallel."
- The training-method name: "reinforcement learning for calibrated decisions
  (RLCD)".
- The `jev-latest` request alias, in the first JSON block below.

Confirmed **absent**: the Score confidence formula. The article states that Score
returns "a confidence value" but never gives its definition, so
`src/veyro/evaluators.py` leaves Score confidence unset with
`confidence_semantics="not_available"` instead of inventing one.

## Verbatim body

Rendered mechanically from `article.content` in the JSON copy: block `type` maps to
a Markdown prefix, `atomic` blocks are replaced by their `entityMap` payload, and
inline link targets are appended as HTML comments. Prose characters are unmodified,
including curly quotes.

---

Agents run in a loop: an LLM decides what to do, a tool executes, a model evaluates the results, and then continues in that loop until the task is complete.

Agents and LLMs were initially difficult to integrate into software applications, which depend on structured data and predictable interfaces. Two primitives emerged that made this much easier:

- Tool calling let models make structured requests and receive structured results.
  <!-- links: [Tool calling](https://openai.com/index/function-calling-and-other-api-updates/) -->

- Structured outputs let models return structured results.
  <!-- links: [Structured outputs](https://www.youtube.com/watch?v=yj-wSRJwrrc) -->

But even with those in place, the agent loop is still slow and costly: every decision requires another model call.

Enter, Jev. Jev is a new model released from TypeSafe AI. The company reports up to 200x faster inference and 400x lower cost than comparable LLMs on classification tasks.
  <!-- links: [released from TypeSafe AI](https://typesafe.ai/blog/introducing-system-one-models-and-jev) -->

[ATOMIC TWEET: {"tweetId": "2099925682726002904"}]

This post covers how Jev works, where it fits into the agent loop, and how to use it with LangChain.

# All about Jev

Jev is actually not a traditional LLM, it doesn’t generate text. It’s what the TypeSafe AI team calls a System One model:

> 📖 System One models are a class of AI models built to make fast, structured decisions that software can use directly. A System One model evaluates a state and returns typed answers and probabilities.
  <!-- links: [state](https://docs.typesafe.ai/concepts/state) -->

It’s trained using reinforcement learning for calibrated decisions (RLCD). Your code uses those results to guide what an agent does next, without a full chat LLM call for each decision.
  <!-- links: [reinforcement learning for calibrated decisions (RLCD)](https://typesafe.ai/blog/introducing-system-one-models-and-jev) -->

To invoke a Jev model, you send it a state (the context) and questions about that state. Here’s a single-question version of the support-ticket example in their docs:
  <!-- links: [their docs](https://docs.typesafe.ai/introduction/quickstart) -->

```json
{
  "model": "jev-latest",
  "state": "Hi, I've been trying to connect my Stripe account for 3 days and it keeps failing. I'm losing sales. Please help ASAP.",
  "questions": {
    "is_urgent": {
      "type": "noul",
      "instructions": "The message conveys urgency or time-sensitivity"
    }
  }
}
```

The docs’ example gives this urgency answer, shown here without the rest of the response:

```json
{
  "is_urgent": {
    "type": "noul",
    "noul": 0.999
  }
}
```

That’s a 99.9% probability that the message is urgent, which your application can use to prioritize the ticket.

There are three types of supported questions:
  <!-- links: [questions](https://www.youtube.com/watch?si=L1qd4LT9W-W67mar&t=216&v=2Bs0Ink_-Uo&feature=youtu.be) -->

[IMAGE: https://pbs.twimg.com/media/HSdZNFLaIAAuezm.jpg]

- Choice: Pick from a set of options. Returns a probability for each option and an overall confidence score.

- Score: Rate an input against ordered levels, such as low, medium, and high. Returns a continuous score, the underlying distribution, and a confidence value.

- Noul: Answer a yes-or-no question. Returns the probability that a statement is true.

One key feature here is that you can ask multiple questions about the same state in one request.

> 💡 System One models evaluate every question in a request in parallel. Adding questions barely changes the response time and costs only the tokens for the extra questions, which are cheap.

For an example of asking multiple questions about a support ticket, see the TypeSafe Quickstart.
  <!-- links: [TypeSafe Quickstart](https://docs.typesafe.ai/introduction/quickstart#request-body) -->

In sum, unlike traditional LLMs, Jev is neither constrained by text generation or sequential decision making!

# How to Use Jev with LangChain

LangChain's provider agnostic model is well suited for supporting Jev alongside thousands of other integrations and model providers.

The LangChain integration exposes Jev through TypeSafeClassifier. You pass your state and questions to .invoke(), and get classification results rather than a chat response.
  <!-- links: [LangChain integration](https://docs.langchain.com/oss/python/integrations/providers/typesafe#quickstart) -->

Install langchain-typesafe and set your TYPESAFE_API_KEY, then make a call:

```python
from langchain_typesafe import Noul, TypeSafeClassifier

classifier = TypeSafeClassifier()

response = classifier.invoke(
    state=(
        "The deploy failed twice and customers are seeing 500s. "
        "Can someone look now?"
    ),
    questions={
        "urgent": Noul(
            instructions="Does this need attention right now?"
        ),
    },
)

urgency = response.nouls["urgent"].noul
```

The state can be text, structured data, or LangChain messages. That makes it straightforward to call Jev from a node or middleware hook using the context your agent already has.

You can build this into custom middleware or tools!

# Use Cases

Jev isn’t a drop-in replacement for an LLM. It doesn’t generate text, but it can handle classification tasks we often use LLMs for today, without the same latency and cost. That makes it a promising complement to the model driving your agent: use an LLM for open-ended reasoning and generation, and Jev for fast, structured decisions along the way.

## Model routing

A simple lookup doesn’t need the same model as a difficult debugging task. Model-routing middleware lets Jev assess the request and choose a model based on criteria you define, so fast and inexpensive for straightforward tasks, more capable for complex ones.
  <!-- links: [Model-routing middleware](https://docs.langchain.com/oss/python/integrations/providers/typesafe#model-routing) -->

```python
from langchain.agents import create_agent
from langchain_typesafe.experimental.middleware import (
    ModelChoice,
    ModelRouterMiddleware,
)

router = ModelRouterMiddleware(
    choices={
        "fast": ModelChoice(
            model="openai:luna",
            criteria="Direct lookups, extraction, and localized changes.",
        ),
        "powerful": ModelChoice(
            model="openai:sol",
            criteria="Architecture and high-stakes decisions.",
        ),
    },
    instructions="Choose the least costly model that can complete the task.",
)

agent = create_agent("openai:gpt-5.6-luna", middleware=[router])
```

The router selects a model from the latest user message and uses it throughout the run. The probabilities and confidence remain available in agent state, too.

## Auto Mode

Agents are still inherently untrustworthy. An agent can receive bad instructions (either naturally or from a motivated enough attacker) which can persuade it into taking actions we didn’t want it to.

Coding harnesses like claude, codex, cursor have shipped some kind of way to classify dangerous actions before they’re taken which has slowly helped to build trust in agents. Up until now, this classifier step has been locked away in the closed source parts of the harness.

Now that a cheap and performant classifier model exists, we can take the same pattern and adopt it to all agents!

```python
from langchain.agents import create_agent
from langchain_typesafe.experimental.middleware import (
    AutoModeMiddleware,
)

guardrail = AutoModeMiddleware(tools=["bash"])

agent = create_agent("openai:gpt-5.6-luna", middleware=[guardrail])
```

AutoModeMiddleware uses Jev to check tool calls for risky decisions it may take, and block calls before the tool executes.
  <!-- links: [AutoModeMiddleware](https://docs.langchain.com/oss/python/integrations/providers/typesafe#tool-risk-gating) -->

# Get Started!

We're pretty thrilled about Jev and the possibilities that come with it. A few cool projects that we’ve seen already: Kyle Jeong from Browserbase is powering browser use agents for fractions of a cent, Jarrod Watts built a live trading agent, and Ryan Vogel is doing email triage at scale.
  <!-- links: [Kyle Jeong](https://x.com/kylejeong/status/2100622054945095934); [Jarrod Watts](https://x.com/jarrodwatts/status/2100356151468585346); [Ryan Vogel](https://x.com/ryanvogel/status/2100042788851101842) -->

New models drop every week at this point, but this one had a pretty outsized response. We’re excited to see what you build with LangChain and Jev.

Let us know what you think on the forum, tag us on X and share what you’re building, or engage with LangChain issues!
  <!-- links: [forum](https://forum.langchain.com/); [X](https://x.com/LangChain?lang=en); [LangChain issues](https://github.com/langchain-ai/langchain) -->

## Acknowledgements

Thanks @huntlovell, @hwchase, @ccurme, @veryboldbagel, and Nathan Drenzer for their thoughtful review and contributions.

---

## Retrieval notes

Verified failures for this URL, recorded so nobody repeats the hunt: the article
page itself and the `Twitterbot`, `facebookexternalhit`, `Slackbot`, `Discordbot`,
and `Googlebot` user-agents return only a generic `og:title` of `X`;
`publish.twitter.com/oembed` returns 404; `cdn.syndication.twimg.com/tweet-result`
returns `{}`; the Wayback CDX API returns zero captures; archive.today holds no
capture; `xcancel.com` returns HTTP 451; every tried Nitter instance is offline.
Only the FixTweet endpoint above returns the body, and only via the
`<screen_name>/status/<tweet_id>` path. The `i/article/<id>` path returns 404.

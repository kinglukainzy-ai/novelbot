THOTH_MODELFILE_INTEGRATION.md
================================

How to wire bot/ai_agent.py to the baked-in "thoth" model
-----------------------------------------------------------

1) Build the model (one-time, repeat after any Modelfile edit):

    ollama create thoth -f Modelfile

2) In .env, change:

    OLLAMA_MODEL=phi4-mini

   to:

    OLLAMA_MODEL=thoth

3) In bot/ai_agent.py, stop sending SYSTEM_INSTRUCTION as a message - the
   "thoth" model already has it baked in via its own SYSTEM block, so
   sending it again from Python would just duplicate it in every prompt
   (wastes tokens, and on a CPU-only box that's a direct latency cost).

   Find this in _run_ollama_agent():

    history = _get_history(user_id)
    messages = ([{"role": "system", "content": SYSTEM_INSTRUCTION}]
                + history + [{"role": "user", "content": text}])

   Replace with:

    history = _get_history(user_id)
    messages = history + [{"role": "user", "content": text}]

   That's the only functional change needed in the tool-calling loop -
   tool schemas, history, and everything else stay exactly as-is.

4) The SYSTEM_INSTRUCTION constant itself: leave it in ai_agent.py, but
   repurpose it. _ask_gemini() still uses it directly (Gemini has no
   Modelfile-equivalent baked-persona mechanism the way Ollama does), so
   don't delete it - just stop passing it into the Ollama messages list.
   Add a short comment above the constant noting it's now also the source
   of truth that Modelfile's SYSTEM block was copied from, so the two
   don't quietly drift apart next time the persona gets tweaked:

    # SYSTEM_INSTRUCTION - used directly by the Gemini fallback path
    # (_ask_gemini). For the Ollama path, this same text is baked into
    # the "thoth" custom model via Modelfile (see repo root) - if you
    # edit this text, copy the same edit into Modelfile and run
    # `ollama create thoth -f Modelfile` again, or the two will drift.
    SYSTEM_INSTRUCTION = (
        ...
    )

5) Sanity check after rebuilding:

    ollama run thoth
    >>> hey
    (should answer in Thoth's voice with no system message sent at all)

    Then restart the bot and try /ask hey - confirm the persona still
    holds and nothing in tool-calling broke.

Net effect
----------
- No speed gain from "baking it in" by itself (same token count either
  way) - this is a maintainability/reliability change, not a performance
  one, exactly as discussed.
- Slightly fewer tokens per call now that the system message isn't
  duplicated in the live messages list AND in the model's own bake - that
  part IS a small, real saving, on top of being more reliable.
- If you ever forget to set OLLAMA_MODEL=thoth and it falls back to
  phi4-mini, the bot will lose the persona entirely (no system message is
  sent from Python anymore) - worth a startup check or a comment in
  .env.example flagging that OLLAMA_MODEL must be "thoth", not the bare
  base model, once you've done this migration.
- Heads up: OLLAMA_MODEL is also read by bot/local_llm.py, so this rename
  affects the scraper's tier-3 fallback and the /ask web_lookup tool too,
  not just the chat persona. Safe in practice (those functions send their
  own system message per call, which overrides the baked one), but worth
  knowing before you go looking for why "thoth" shows up in `ollama ps`
  during a scheduled check cycle.
- The Modelfile also adds a few MESSAGE few-shot examples aimed at
  phi4-mini's two documented quirks (rambling past a short answer;
  emitting tool calls as plain text instead of using the structured
  tool_calls field). Treat this as an experiment - run a few /ask turns
  before and after to see if it actually helps on your hardware, since
  small models don't all respond to few-shot priming the same way.


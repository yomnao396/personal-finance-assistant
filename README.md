# Personal Finance AI Assistant

A bilingual (English / Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©) personal finance assistant. It analyses income and
spending, forecasts next month, builds a budget, and produces a concrete plan to
reach a savings goal. A local AI assistant (Qwen through Ollama) answers questions
using only numbers calculated by the finance engine.

## Run it

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
./run_demo.sh                      # API on :8000, app on http://localhost:8501
```

Without the script, start the two parts in two terminals:

```bash
uvicorn app.api.main:app --port 8000
streamlit run streamlit_app.py
```

In the app, open **Load demo data** in the sidebar. It loads:
- the sample user (three months of data, two goals and essential-spending preferences);
- profiles Aâ€“E from the Upgrade Report: five budgets, from high income down to overspending;
- a new user who has only completed onboarding.

Switch between them with **Profile** in the sidebar.

`python run.py` is another way to set things up. It creates the tables, seeds the sample user and checks that the model exists.

### The AI assistant (optional)

```bash
ollama serve
ollama pull qwen2.5:3b
```

Every other page works without it. The **Ask the assistant** page says when Ollama is
offline. To use a different model or host, set `OLLAMA_MODEL` or `OLLAMA_HOST`.

## Demo walkthrough

1. **This month**: one sentence sums up the month, and the bar under it shows where the income went.
2. **Analysis**: spending by category, trends, income stability, and next month's forecast.
3. **Essential spending**: drag *Restaurant* into **Essential**, then open **Your plan**. The plan stops cutting it and changes immediately, with no retraining.
4. **Goals**: drag *Emergency fund* above *Laptop and course*. The plan is rebuilt around the new top goal.
5. **Your plan**: numbered steps with exact amounts. Open *How this plan was decided* to see the Random Forest's confidence for each action.
6. **Goals â†’ Can I afford it?** "A 5,000 laptop in 4 months" gives a probability, a Likely/Borderline/Unlikely label and an action. Then try **What ifâ€¦**: cut a category, save more, change income, or wait longer.
7. Switch **Profile** to *C â€” tight budget* and ask the same question. The answer is different because it uses that profile's money. Then switch to *New user â€” onboarding only*: the answer says it is "based on what you entered".
8. **Money tips**: search the verified notes in English or Arabic. Each note shows its source.
9. Switch the language to **Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©**. The whole interface, including the plan, goes right-to-left in Arabic.

## How it fits together

```
Streamlit UI (ui/)  â”€â”€HTTPâ”€â”€â–¶  FastAPI (app/api/main.py)
                                 â”œâ”€ database/     SQLite: users, income, transactions, goals, preferences
                                 â”œâ”€ finance/      pure calculations and trend analysis
                                 â”œâ”€ engines/      financial analysis, budget, goals, recommendation plan
                                 â”œâ”€ ml/           features, Random Forest (which actions), spending forecast
                                 â”œâ”€ preferences/  essential vs flexible (a runtime constraint, not a model input)
                                 â”œâ”€ services/     intent + direct answers (the assistant's fast path)
                                 â””â”€ llm/          Qwen agent that calls tools in app/tools/
```

- **The assistant** answers questions about your own numbers without the model. `app/services/intent.py`
  reads the question (English or Arabic): the intent, the period, the category and, for a what-if, the
  exact changes. `app/services/direct_answers.py` then computes the answer from the database
  (`app/finance/facts.py`: one user, one period, exact Decimal sums) and the existing engines, and
  writes it as a checked sentence in milliseconds. Each answer keeps its context (goal, period,
  category, what-if) for follow-ups and its calculation for "how did you calculate that?". A what-if
  changes only what you name and never writes to the database. When a needed detail is missing
  ("and if I reduce it?", "raise it 15%" with nothing earlier to refer to, two values for one thing)
  the assistant asks one short question instead of guessing. Savings plans add up to exactly the
  amount asked for, and sums written by the model are checked (`app/finance/verify.py`). "How much
  did I earn, spend and have left?" is answered part by part from one month's records, "show me the
  transactions" lists the records with their verified total, and "where did that number come from?"
  works the figure out again from the records. When the model answers a question about your own
  numbers, every amount it writes must be in your records and every value you asked for must be
  there; otherwise the answer is rebuilt from the records. What-ifs read which variable changes from
  the words next to the change ("if my spending rises 300, how much will I save?" changes spending),
  keep "300 more", "300 in total" and "save 300 more" apart (and ask when it is unclear), and build on
  the previous scenario only when asked ("another 200"). Goals overview, next-month forecast, daily and
  weekly averages, trends, unusual spending, the largest transaction and "X% of my income" are answered
  from the records too. Forecast
  questions ("next month") are not answered with this month's actual figures. General money
  questions, and anything the parser does not clearly understand, go to Qwen as before.

- **Decision support** (`app/finance/decisions.py`, `app/services/decision_answers.py`) helps with a
  choice, not only a number, again without the model. It keeps money buckets apart so savings are never
  mistaken for free money:
  - *left each month* is income âˆ’ spending;
  - *unallocated* is that minus what your dated goals need each month;
  - *savings outside goals* is free unless part of it is needed for the safety floor (one month of
    essential spending; your emergency goal counts towards it);
  - *money inside goals* is earmarked.

  Any cost is covered from these buckets in a fixed order, and the verdict says which buckets it had
  to touch:
  - **Affordable**: unallocated money and free savings only;
  - **Affordable with adjustments**: also this month's goal contributions or cuts to flexible
    spending (never essential spending);
  - **Risky**: also emergency savings;
  - **Not currently affordable**;
  - **Insufficient information**.

  On top of this the assistant handles:
  - **Purchases** ("can I afford a new phone?", "buy this laptop now or wait?", "in 6 installments?",
    "a loan for a car?", "how much can I safely spend?"): asks for the price if it is missing, then
    compares buying now, waiting, a cheaper option and installments or a loan (with the total cost);
  - **Trade-offs** with a named goal ("a 5,000 phone, but I also want my emergency fund"): adds
    "save for both" and follows the priority you state;
  - **Planned expenses** ("prepare for Ramadan expenses"): the monthly amount to set aside;
  - **Emergencies**: the least-harm way to cover them, with a recovery plan;
  - **Goal priority**: one goal at a time vs splitting vs emergency fund first, using your saved
    order when there is one;
  - **Saving plans**: limits from your own earlier months, never generic percentages.

  Every answer has the same parts: the recommendation and why, your verified numbers, the options,
  risks, assumptions, what is still missing, and the next step. Follow-ups continue from it ("what if
  I wait 3 months?", "a cheaper one for 2,000?", "compare it with the previous option").

  Real changes (`app/services/actions.py`) work like this:
  - Requests such as "add 500 to my emergency fund", "move the laptop deadline to June 2027", "make
    the emergency fund my top priority" and "record 200 on lunch" change nothing until you reply
    *yes* (or press the button in the chat).
  - The change is then validated again (it must be your goal, a positive amount, a future date and a
    valid category), written, and read back before it is reported as done.
  - "What if I add 500 to my emergency fund?" only shows the effect.

  The read-only API routes are `/decisions/position`, `/decisions/purchase`, `/decisions/emergency`,
  `/decisions/goal-priorities` and `/decisions/saving-plan`. On the Afford page, "Buy now, wait, or
  pay in parts?" shows the same comparison.

  For model answers, an income figure that contradicts your records is never shown, and a request
  for figures the records already hold is retried, then removed.
- **Reasoning over several turns** (`app/services/intent.py`, `app/services/direct_answers.py`):
  - **Several categories.** "Reduce restaurants and fast food by 15%" changes both categories
    (800 + 400 = 1,200; 1,200 Ã— 15% = 180) and shows the rest of the spending as unchanged. "How
    much did I spend on restaurants and fast food?" sums every record of both.
  - **Each or in total?** "Cut restaurants and fast food by 300" asks whether you mean 300 from each
    or 300 in total. In total, the 300 is shared in proportion to what each category costs.
  - **Revising the last scenario.** "What if I increase the reduction to 20%?" (or "Ù„Ùˆ Ø®Ù„ÙŠØªÙ‡Ø§ 20%")
    keeps the same categories and direction with the new size. "Keep all other expenses unchanged"
    is understood as a constraint, not as the Others category.
  - **Corrections.** "You calculated only restaurants, but I meant restaurants and fast food
    together" names the mistake and recalculates.
  - **Pronouns, typos, mixed language.** "Reduce them by 15%" / "Ù‚Ù„Ù„Ù‡Ù… 10%" after an answer about two
    categories applies to both; "resturants", "fastfood", "Ø§Ù„Ù€restaurants" and "Ø§Ù„ÙØ§Ø³Øª ÙÙˆØ¯" are
    understood; "what about last month?" reruns the scenario on that month; "and transport by 10%
    too" adds to it.
  - **Not handed to the model.** A scenario follow-up that isn't clear gets one question back,
    never a guess.
  - **Checked before it's shown.** Every scenario answer is compared with the request (every named
    category must be in it), and every "a + b = c" in a fast answer is checked.
  - **No unrelated goals.** A goal is added only when it is asked about.
  - **Category analysis.** "Analyze my food expenses" compares with the month before, separates
    essential from flexible spending, and names the saving that is actually available.
  - **Cut choices and saving targets.** "Restaurants or transport: what should I cut first?" and
    "save 1,000 by cutting only food, is that realistic?" are judged from the records (realistic
    cut = 30% of each flexible line), and an unrealistic target is called one.
- **Purchase plans across a conversation** (`app/services/plan_followups.py`, `assess_plan` in
  `app/finance/decisions.py`):
  - **One plan, kept and corrected.** "A PlayStation for 15,000 in four months" becomes a plan (item,
    price, months) that every follow-up starts from. "Actually, the price is 10,000" or "Ù„Ø§ Ø³Ø¹Ø±Ù‡Ø§ 10 Ø§Ù„Ù
    Ø¨Ø³" changes only the price and keeps the four months. Nothing already given is asked again, and an
    answer about something else in between does not lose the plan.
  - **Four questions kept apart.** Does the monthly amount fit in what is left after spending? Does it
    fit without reducing what the goals receive now? How much would each goal fall behind? What would
    close the gap: realistic cuts to flexible spending, extra income, or more months?
  - **Short follow-ups.** "Is it possible?", "how much a month?", "what if I reduce my shopping?",
    "and if I earn 1,000 more?" and "how does it affect my goals?" get short answers to that question.
    The full comparison of options is given only when asked for.
  - **When the model answers.** If a question about the plan goes to the model, the app runs the plan
    assessment itself and the model only explains it. The model is told the facts already established,
    earlier versions of the plan are kept out of its history, and sentences with figures that neither
    the records nor the current plan support are removed. Identical tool calls in one answer run once.
- **Processing diagnostics** (`app/services/trace.py`). Every chat answer reports which stages ran
  (interpret, retrieve and calculate, validate; or context, model generation, tools and validate),
  how long each took, and how many database queries, records and model calls it needed. The
  report goes in the `/chat` response as `processing` and in one console log line, with no amounts
  or text. A fast answer still shows its database reads: it is fast because the arithmetic is done
  in code, not because steps are skipped.
- **Random Forest** (`app/ml/recommendation_model.py`) picks *which* actions apply. The engine (`app/engines/recommendation_engine.py`) works out *how much*, from real spending and the goal gap.
- **Forecast** (`app/ml/prediction.py`) fits a linear trend to each category over the last 6 months and caps it at 1.5Ã— the highest month.
- **Goal priority**: goals you ranked come first. Otherwise the plan targets the goal with the earliest date.
- **Spending forecast with uncertainty** (`app/ml/prediction.py`) has three data states:
  - *entered*: onboarding estimates only, uncertainty Â±15%;
  - *recent*: 1â€“2 months, the average, uncertainty Â±15%;
  - *pattern*: 3+ months, LinearRegression, with uncertainty from the regression's prediction interval and a Student-t correction.
- **Goal assessment** (`app/ml/goal_assessment.py`) works out P(savings â‰¥ target) with a normal approximation:
  - labels: Likely at 70% or more, Borderline at 40% or more, otherwise Unlikely;
  - the suggested action targets 80% confidence;
  - it reproduces the Upgrade Report's worked example exactly.
- **What-if and goal ranking** (`app/services/scenario.py`) rerun the assessment with one input changed. Goals are ranked by importance Ã— urgency. Both are deterministic, as the Intelligence Design specifies.
- **Onboarding** (`app/services/onboarding.py`) stores a new user's estimates as `is_estimate` transactions. Real spending replaces them automatically.
- **Knowledge base (RAG)** (`app/rag/knowledge.py`, `data/knowledge/knowledge_base.json`):
  - 11 entries, written in English and Arabic, each paraphrasing an official page (CFPB, FDIC, IMF, Duke University);
  - all source pages were checked on 2026-10-25;
  - passages are stored as TF-IDF vectors and searched by cosine similarity;
  - below a measured threshold the assistant says it cannot answer;
  - to change it, edit the JSON; the index rebuilds itself.
- **Chat** shows which tools produced each answer and which sources back it.
- **CSV import** (`app/services/importer.py`) is on the Spending page and validates every row.

## Interface

| Folder / file | Contents |
|---|---|
| `ui/i18n.py` | Every interface string in English and Arabic, plus category names. Add a language by adding a block to `TEXT`. |
| `ui/theme.py` | Colours, IBM Plex Sans Arabic font, right-to-left layout, shared building blocks |
| `ui/charts.py` | Altair charts. Each category keeps one colour on every chart. |
| `ui/board.py` | Styling for the drag-and-drop boards (`streamlit-sortables`) |
| `ui/views/` | One file per page |

Amounts are in Sudanese pounds: SDG in English, Ø¬.Ø³ in Arabic. To change the currency, edit `CURRENCY` in `ui/i18n.py` and the currency line in `app/llm/prompts.py`.

## Evaluation (proposal Table 3.2)

| Script | What it measures | Needs Ollama |
|---|---|---|
| `python -m scripts.evaluate_assistant` | numeric accuracy (tools on vs off), hallucination rate and correct "cannot answer" (RAG on vs off), source use, all split by easy/hard, over the 30 questions in `data/evaluation/questions.json` | yes |
| `python -m scripts.fairness_check` (`--llm` to also compare chat answers) | identical finances under different names get identical results; profiles Aâ€“E are ordered sensibly | only with `--llm` |
| `python -m scripts.validate_forecast` | forecast MAE/RMSE/MAPE against a "same as last month" baseline, and calibration of the Â±1Ïƒ/Â±2Ïƒ ranges, on 100 synthetic users | no |
| `python -m scripts.summarize_questionnaire` | SUS score and trust items from the user study (`docs/evaluation/usability_questionnaire.md`, EN/AR) | no |
| `python -m scripts.demo_outputs` | prints the whole pipeline, sections 1â€“14 | no |

Reports are written to `reports/`. The fairness and forecast reports already there are real results.

Forecast results:
- **The ranges are well calibrated.** 68â€“79% of actual values fall inside Â±1Ïƒ (ideal 68%) and 92â€“97% inside Â±2Ïƒ (ideal 95%).
- **The point forecast only beats the naive baseline at 11 months.** At 3â€“6 months of history the regression is less accurate than "same as last month". Report this rather than hide it.

The assistant benchmark has **not** been run yet because it needs Ollama. Run it on your machine.

**Option 1 dataset (Kaggle):** `scripts/kaggle_to_scenarios.py` is the converter from the Update Manual, and its tests pass. To run it you still need to download a Kaggle CSV into `data/kaggle/`:

```bash
python -m scripts.kaggle_to_scenarios --csv data/kaggle/<file>.csv --inspect
```

## Tests

```bash
pytest
```

The test suite covers the database, calculations, financial and recommendation engines, forecasting, goal assessment, scenarios, onboarding, the knowledge base, data importing, assistant responses, routing regressions, API behavior, and translations. Run the tests with `pytest`.

To retrain the model: `python -m scripts.train_recommendation_model --regenerate`



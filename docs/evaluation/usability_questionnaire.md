# Usability and trust questionnaire / استبيان سهولة الاستخدام والثقة

For the proposal's user evaluation (Table 3.2: "Usefulness and trust — short
questionnaire and a few open questions after use", about 6–8 volunteers).

**Procedure (about 20 minutes per person)**

1. Load the demo data. Give the participant these tasks without help:
   - find how much they spent on food this month;
   - check whether they can afford a 5,000 laptop in 4 months;
   - mark *Restaurant* as essential and see how the plan changes;
   - ask the assistant one general money question (for example, "what is an emergency fund?") and look at its source.
2. Then they fill in Part A and Part B, and answer Part C aloud or in writing.
3. Enter the answers in `data/evaluation/questionnaire_responses.csv` (copy the
   template next to it), then run `python -m scripts.summarize_questionnaire`.

Scale for every item: 1 = strongly disagree … 5 = strongly agree.
المقياس لكل عبارة: 1 = لا أوافق بشدة … 5 = أوافق بشدة.

## Part A — System Usability Scale (SUS) / مقياس سهولة الاستخدام

The ten standard SUS statements (Brooke, 1996). The Arabic wording is our own
translation; if you use a published, validated Arabic SUS instead, cite it.

| # | English | العربية |
|---|---|---|
| SUS1 | I think that I would like to use this system frequently. | أعتقد أنني سأرغب في استخدام هذا النظام بشكل متكرر. |
| SUS2 | I found the system unnecessarily complex. | وجدت النظام معقدًا بلا داعٍ. |
| SUS3 | I thought the system was easy to use. | رأيت أن النظام سهل الاستخدام. |
| SUS4 | I think that I would need the support of a technical person to be able to use this system. | أعتقد أنني سأحتاج إلى مساعدة شخص تقني لأتمكن من استخدام هذا النظام. |
| SUS5 | I found the various functions in this system were well integrated. | وجدت أن وظائف النظام المختلفة متكاملة بشكل جيد. |
| SUS6 | I thought there was too much inconsistency in this system. | رأيت أن في النظام قدرًا كبيرًا من عدم الاتساق. |
| SUS7 | I would imagine that most people would learn to use this system very quickly. | أتصور أن معظم الناس سيتعلمون استخدام هذا النظام بسرعة كبيرة. |
| SUS8 | I found the system very cumbersome to use. | وجدت استخدام النظام مرهقًا جدًا. |
| SUS9 | I felt very confident using the system. | شعرت بثقة كبيرة أثناء استخدام النظام. |
| SUS10 | I needed to learn a lot of things before I could get going with this system. | احتجت إلى تعلم أشياء كثيرة قبل أن أبدأ استخدام هذا النظام. |

SUS score: odd items contribute (answer − 1), even items (5 − answer); the sum × 2.5
gives 0–100. The summary script does this.

## Part B — Usefulness and trust / الفائدة والثقة

These map to RQ4 ("Do users find the assistant useful and trustworthy, especially
when it shows the reason and the source behind its advice?").

| # | English | العربية |
|---|---|---|
| T1 | The assistant's advice was useful for my own situation. | كانت نصائح المساعد مفيدة لوضعي الخاص. |
| T2 | I trusted the numbers the assistant gave me. | وثقت بالأرقام التي قدمها المساعد. |
| T3 | Seeing the reason behind the advice made me trust it more. | رؤية سبب النصيحة جعلتني أثق بها أكثر. |
| T4 | Seeing the source of a fact made me trust it more. | رؤية مصدر المعلومة جعلتني أثق بها أكثر. |
| T5 | When the assistant said it could not answer, that increased my trust. | عندما قال المساعد إنه لا يستطيع الإجابة زادت ثقتي به. |
| T6 | The plan respected the expenses I marked as essential. | احترمت الخطة المصروفات التي حددتها كأساسية. |
| T7 | Using the app in my preferred language (Arabic or English) was comfortable. | كان استخدام التطبيق بلغتي المفضلة (العربية أو الإنجليزية) مريحًا. |

## Part C — Open questions / أسئلة مفتوحة

- O1. What did you find most useful? / ما أكثر شيء وجدته مفيدًا؟
- O2. Was there any answer you did not trust? Why? / هل هناك إجابة لم تثق بها؟ لماذا؟
- O3. What would make you use this regularly? / ما الذي سيجعلك تستخدمه بانتظام؟
- O4. Anything confusing or missing? / هل هناك شيء مربك أو ناقص؟

## Participant details (no names) / بيانات المشارك (بدون أسماء)

Participant code (P1, P2…), knows about budgeting (yes/no), language used (ar/en).

# Test recordings for recording → AI actions

123 cases. Each runs as a recording from the test account in `fixture.json` (Ali, Sara, Rami, Nour, Mom; habits Gym, Read, Meditation, Walk; projects Website redesign, Kitchen renovation; goals Run a 10k, Save 5000; Rent, Netflix and yesterday's Lunch with Rami; Rami owes 50).

A case passes when vournal AI proposes exactly the listed changes (reading/searching is always fine), with the right amounts, people and dates, asks when the row says it must, and proposes nothing else. To-dos are not listed: those come from the normal reading, not from AI actions.

| category | cases | en | lb | az | ar |
|---|---|---|---|---|---|
| money | 21 | 8 | 5 | 5 | 3 |
| debts | 8 | 3 | 2 | 2 | 1 |
| habits | 16 | 8 | 3 | 3 | 2 |
| projects | 9 | 4 | 2 | 2 | 1 |
| goals | 6 | 3 | 1 | 1 | 1 |
| people | 14 | 6 | 3 | 3 | 2 |
| health | 9 | 4 | 2 | 2 | 1 |
| edits | 7 | 4 | 1 | 1 | 1 |
| deletes | 4 | 2 | 1 | 1 | 0 |
| multi | 6 | 3 | 1 | 1 | 1 |
| diary | 10 | 4 | 2 | 2 | 2 |
| todos-only | 4 | 1 | 1 | 1 | 1 |
| ask | 7 | 4 | 1 | 1 | 1 |
| guard | 2 | 2 | 0 | 0 | 0 |

## money

- **money-en-01** — Grabbed lunch with Rami today, paid 20 dollars.
  - expected: expense 20 USD (lunch)
- **money-en-02** — Filled up the car, 45 bucks. Gas is getting so expensive.
  - expected: expense 45 USD (gas/fuel/petrol/car)
- **money-en-03** — Got paid today, 1800 salary came in.
  - expected: income 1,800 USD (salary/pay)
- **money-en-04** — Yesterday I spent 32 on groceries at Spinneys.
  - expected: expense 32 USD (groceries/spinneys/supermarket), dated {yesterday}
- **money-en-05** — Paid the electricity bill, 60 dollars, and internet was 35.
  - expected: expense 60 USD (electric); expense 35 USD (internet/wifi)
- **money-en-06** — Freelance client sent me 400 for the logo work.
  - expected: income 400 USD (logo/freelance/client)
- **money-en-07** — Coffee this morning, 4 dollars. Then a taxi home for 8.
  - expected: expense 4 USD (coffee); expense 8 USD (taxi/cab/ride)
- **money-en-08** — Bought a new phone case online for 15.
  - expected: expense 15 USD (phone/case)
- **money-lb-01** — دفعت ٢٠ دولار عالغدا مع رامي اليوم
  - expected: expense 20 USD (غدا/lunch/غداء)
- **money-lb-02** — عبيت بنزين بمية وخمسين ألف
  - expected: expense 150,000 LBP (بنزين/fuel/gas/benzine)
- **money-lb-03** — قبضت المعاش اليوم، ١٨٠٠ دولار
  - expected: income 1,800 USD (معاش/salary)
- **money-lb-04** — مبارح صرفت ٣٠ دولار عالخضرة
  - expected: expense 30 USD (خضرة/vegetables/groceries/خضار), dated {yesterday}
- **money-lb-05** — دفعت فاتورة الكهربا ٦٠ دولار وفاتورة الانترنت ٢٥
  - expected: expense 60 USD (كهربا/electric); expense 25 USD (انترنت/internet)
- **money-az-01** — dafa3et 20$ 3al ghada ma3 Rami el yom
  - expected: expense 20 USD (ghada/lunch)
- **money-az-02** — 3abbayt benzine b 150 alf
  - expected: expense 150,000 LBP (benzine/fuel/gas)
- **money-az-03** — 2abadt el ma3esh el yom, 1800$
  - expected: income 1,800 USD (ma3esh/salary)
- **money-az-04** — sarraft 10$ 3a ahwe w kaak el seb7
  - expected: expense 10 USD (ahwe/coffee/kaak)
- **money-az-05** — shtaret jezde jdide b 70$
  - expected: expense 70 USD (jezde/jezdan/wallet)
  - note: jezde/jezdan is a wallet
- **money-ar-01** — دفعت اليوم عشرين دولاراً ثمن الغداء مع رامي.
  - expected: expense 20 USD (الغداء/lunch)
- **money-ar-02** — استلمت راتبي اليوم، ألف وثمانمئة دولار.
  - expected: income 1,800 USD (راتب/salary)
- **money-ar-03** — اشتريت كتاباً بخمسة عشر دولاراً.
  - expected: expense 15 USD (كتاب/book)

## debts

- **debt-en-01** — I owe Nour 30 dollars for the concert ticket, need to pay her back.
  - expected: create_debt: direction=i_owe, amount=30, person_id=Nour
- **debt-en-02** — Rami paid me back 20 of the 50 he owes me.
  - expected: add_debt_payment: debt_id=Rami, amount=20
- **debt-en-03** — Lent Ali 100 dollars today, he'll return it next month.
  - expected: create_debt: direction=owed_to_me, amount=100, person_id=Ali
- **debt-lb-01** — رامي رجعلي ٢٠ دولار من ال٥٠ يلي إلي عندو
  - expected: add_debt_payment: debt_id=Rami, amount=20
- **debt-lb-02** — دينت علي ١٠٠ دولار، رح يرجعن الشهر الجاي
  - expected: create_debt: direction=owed_to_me, amount=100, person_id=Ali
- **debt-az-01** — ana mdayan la Nour b 30$ la tazkart el concert
  - expected: create_debt: direction=i_owe, amount=30, person_id=Nour
- **debt-az-02** — Rami raja3le kel el masare li kenet 3endo, el 50$
  - expected: add_debt_payment: debt_id=Rami, amount=50
- **debt-ar-01** — أقرضت علي مئة دولار اليوم.
  - expected: create_debt: direction=owed_to_me, amount=100, person_id=Ali

## habits

- **habit-en-01** — Went to the gym this morning, legs day. Feeling great.
  - expected: check off Gym
- **habit-en-02** — Read 20 pages before bed tonight.
  - expected: check off Read ×20
- **habit-en-03** — Did my meditation and then went to the gym.
  - expected: check off Meditation; check off Gym
- **habit-en-04** — Skipped the gym today, way too tired.
  - expected: nothing · *(no AI actions step)*
  - note: skipping is not a check-in
- **habit-en-05** — I want to start drinking 8 glasses of water every day, track that for me.
  - expected: create_habit (water): target_per_day=8
- **habit-en-06** — Walked about 12000 steps today.
  - expected: check off Walk
  - note: the Walk habit is a plain daily check; the step count isn't required
- **habit-en-07** — Yesterday I meditated, forgot to log it.
  - expected: check off Meditation on {yesterday}
- **habit-en-08** — Let's make reading 30 pages a day instead of 20.
  - expected: update_habit: id=Read, target_per_day=30
- **habit-lb-01** — رحت عالجيم الصبح
  - expected: check off Gym
- **habit-lb-02** — قريت ٢٠ صفحة قبل ما نام
  - expected: check off Read ×20 on {today}|{yesterday}
  - note: 'before I slept' can mean last night
- **habit-lb-03** — ما رحت عالجيم اليوم، كنت تعبان كتير
  - expected: nothing · *(no AI actions step)*
- **habit-az-01** — re7et 3al gym el seb7, kan fi zaw2
  - expected: check off Gym
- **habit-az-02** — 3melt meditation w 2reet 20 safha
  - expected: check off Meditation; check off Read ×20
- **habit-az-03** — badde ballesh rkod kel yom, 7otle yeh ka habit
  - expected: create_habit (run/rkod/jog)
- **habit-ar-01** — ذهبت إلى النادي الرياضي هذا الصباح.
  - expected: check off Gym
- **habit-ar-02** — تأملت لمدة عشر دقائق اليوم.
  - expected: check off Meditation

## projects

- **project-en-01** — Starting a new project called Vournal 2, deadline end of January.
  - expected: create_project (vournal 2): deadline=*-01-31 or *-01-30 or *-01-29
- **project-en-02** — The website redesign is finally done, shipped it today!
  - expected: update_project: id=Website redesign, status=completed
- **project-en-03** — Put the kitchen renovation on hold until summer, no budget right now.
  - expected: update_project: id=Kitchen renovation, status=on_hold
- **project-en-04** — New project: plan Sara's wedding gift. Budget around 300 dollars.
  - expected: create_project (gift/wedding): budget=300
- **project-lb-01** — بدي ابلش مشروع جديد اسمو فوكال ٢
  - expected: create_project (2)
  - note: name as spoken; any title containing 2
- **project-lb-02** — خلصنا مشروع تصميم الموقع اليوم أخيراً
  - expected: update_project: id=Website redesign, status=completed
- **project-az-01** — khallasna el website redesign akhiran
  - expected: update_project: id=Website redesign, status=completed
- **project-az-02** — badde balesh project jdid esmo Garden, el deadline akher el shahr
  - expected: create_project (garden): deadline=*-11-30 or *-11-29
- **project-ar-01** — أوقفنا مشروع تجديد المطبخ مؤقتاً.
  - expected: update_project: id=Kitchen renovation, status=on_hold

## goals

- **goal-en-01** — New goal: learn Spanish by next summer.
  - expected: create_goal (spanish)
- **goal-en-02** — I've saved 1200 so far toward the 5000.
  - expected: update_goal: id=Save 5000, current_value=1200
- **goal-en-03** — Ran the 10k today, goal achieved!
  - expected: update_goal: id=Run a 10k, status=completed
  - note: a run may also log nothing else
- **goal-lb-01** — هدف جديد: بدي اتعلم اسباني قبل الصيفية الجاية
  - expected: create_goal (اسباني/spanish)
- **goal-az-01** — waffart 1200$ la hala men el 5000
  - expected: update_goal: id=Save 5000, current_value=1200
- **goal-ar-01** — هدفي الجديد أن أقرأ عشرين كتاباً هذا العام.
  - expected: create_goal (كتاب/books/read)

## people

- **people-en-01** — Visited Ali today and gave him a book as a gift.
  - expected: gift for Ali (book), given
- **people-en-02** — Met my new neighbor Karim and brought him some sweets.
  - expected: gift for new person Karim or كريم (sweet), given; update_person: id=new person Karim or كريم *(optional)*
  - note: noting the new person as a neighbor is welcome
- **people-en-03** — Sara's birthday is on March 14, don't let me forget.
  - expected: add_important_date (birthday): person_id=Sara, date=*-03-14
  - note: any year, but 14 March
- **people-en-04** — Gift idea for Mom: a silk scarf, maybe 40 dollars.
  - expected: gift for Mom (scarf), idea
- **people-en-05** — Nour told me she's allergic to peanuts, I should remember that.
  - expected: update_person (peanut): id=Nour
  - note: the person's notes must mention peanuts
- **people-en-06** — Bought Rami a birthday gift, a mug, 18 dollars.
  - expected: gift for Rami (mug), given; expense 18 USD *(optional)*
  - note: the expense for the mug is welcome but not required
- **people-lb-01** — زرت علي اليوم وجبتلو كتاب هدية
  - expected: gift for Ali (كتاب/book), given
- **people-lb-02** — عيد ميلاد سارة ١٤ آذار
  - expected: add_important_date (عيد/birthday): person_id=Sara, date=*-03-14
- **people-lb-03** — تعرفت عجاري الجديد كريم وجبتلو حلو
  - expected: gift for new person كريم or Karim (حلو/sweet), given; update_person: id=new person كريم or Karim *(optional)*
  - note: noting the new person as a neighbor is welcome
- **people-az-01** — zert Ali el yom w jebtlo ktab hdiye
  - expected: gift for Ali (ktab/book), given
- **people-az-02** — 3id milad Sara b 14 adar
  - expected: add_important_date (3id/birthday): person_id=Sara, date=*-03-14
- **people-az-03** — fikra la hdiye la mama: echarpe, chi 40$
  - expected: gift for Mom or new person mama or new person ماما (echarpe/scarf), idea
  - note: Mom, or a new person called mama, both pass
- **people-ar-01** — زرت علي اليوم وأهديته كتاباً.
  - expected: gift for Ali (كتاب/book), given
- **people-ar-02** — عيد ميلاد سارة في الرابع عشر من آذار.
  - expected: add_important_date (عيد/birthday): person_id=Sara, date=*-03-14

## health

- **health-en-01** — Had a chicken shawarma for lunch.
  - expected: log_food (shawarma)
- **health-en-02** — Drank about 2 liters of water today.
  - expected: log_water: ml=2000
- **health-en-03** — Weighed myself this morning, 78 kilos.
  - expected: log_weight: kg=78
- **health-en-04** — Breakfast was two eggs and toast, then a salad for dinner.
  - expected: log_food (egg/toast); log_food (salad)
- **health-lb-01** — تغديت شاورما دجاج
  - expected: log_food (شاورما/shawarma)
- **health-lb-02** — وزنت حالي الصبح، ٧٨ كيلو
  - expected: log_weight: kg=78
- **health-az-01** — shrebt 2 litre may el yom
  - expected: log_water: ml=2000
- **health-az-02** — akalt man2oushe za3tar 3al terwi2a
  - expected: log_food (man2oushe/manakish/zaatar/za3tar/منقوشة)
- **health-ar-01** — تناولت سلطة فتوش على العشاء.
  - expected: log_food (فتوش/fattoush/salad/سلطة)

## edits

- **edit-en-01** — Actually yesterday's lunch with Rami was 25, not 20.
  - expected: update_transaction: id=Lunch, amount=25
- **edit-en-02** — Rename the kitchen renovation project to Kitchen and dining room.
  - expected: update_project (dining): id=Kitchen renovation
- **edit-en-03** — Rent this month was actually 550, the landlord added the generator fee.
  - expected: update_transaction: id=Rent, amount=550
- **edit-en-04** — Move the website redesign deadline to December 20.
  - expected: update_project: id=Website redesign, deadline=*-12-20
- **edit-lb-01** — الغدا مع رامي مبارح كان ٢٥ مش ٢٠
  - expected: update_transaction: id=Lunch, amount=25
- **edit-az-01** — el ghada ma3 Rami mbere7 kan 25$ mish 20
  - expected: update_transaction: id=Lunch, amount=25
- **edit-ar-01** — إيجار هذا الشهر كان خمسمئة وخمسين وليس خمسمئة.
  - expected: update_transaction: id=Rent, amount=550

## deletes

- **delete-en-01** — Delete the Netflix expense, I cancelled it and they refunded me.
  - expected: delete transaction Netflix
- **delete-en-02** — I'm done with the walking habit, remove it.
  - expected: delete habit Walk (or archive it)
  - note: deleting or archiving the habit both pass
- **delete-lb-01** — امحيلي مصروف النتفليكس
  - expected: delete transaction Netflix
- **delete-az-01** — shil el Netflix men el masarif
  - expected: delete transaction Netflix

## multi

- **multi-en-01** — Busy day. Gym in the morning, then lunch with Rami, I paid 20. In the evening I visited Ali and gave him a book. Also read 20 pages before bed.
  - expected: check off Gym; expense 20 USD (lunch); gift for Ali (book), given; check off Read ×20
- **multi-en-02** — Got my salary, 1800. Paid rent 500 and put 300 toward savings, so I'm at 1500 for the 5000 goal now.
  - expected: income 1,800 USD (salary); expense 500 USD (rent); update_goal: id=Save 5000, current_value=1500; money 300 USD *(optional)*
  - note: the 300 toward savings may be recorded or not
- **multi-en-03** — Finished the website redesign today, celebrated with dinner, 45 dollars. Starting a new project tomorrow called Mobile app.
  - expected: update_project: id=Website redesign, status=completed; expense 45 USD (dinner); create_project (mobile app)
- **multi-lb-01** — نهار طويل. رحت عالجيم، تغديت مع رامي ودفعت ٢٠ دولار، وبالليل زرت علي وجبتلو كتاب
  - expected: check off Gym; expense 20 USD (غدا/lunch); gift for Ali (كتاب/book), given
- **multi-az-01** — re7et 3al gym, w dafa3et 20$ 3al ghada ma3 Rami, w bel lel zert Ali w jebtlo ktab
  - expected: check off Gym; expense 20 USD (ghada/lunch); gift for Ali (ktab/book), given
- **multi-ar-01** — ذهبت إلى النادي صباحاً، ثم تغديت مع رامي ودفعت عشرين دولاراً، وفي المساء زرت علي وأهديته كتاباً.
  - expected: check off Gym; expense 20 USD (غداء/lunch/الغداء); gift for Ali (كتاب/book), given

## diary

- **diary-en-01** — Feeling kind of low today. Work was stressful and I just want to sleep.
  - expected: nothing · *(no AI actions step)*
- **diary-en-02** — Had a long talk with Sara about our childhood. It was really nice.
  - expected: nothing · *(no AI actions step)*
- **diary-en-03** — The weather was beautiful, I sat on the balcony and watched the sunset.
  - expected: nothing · *(no AI actions step)*
- **diary-en-04** — I keep thinking about whether I should change jobs. Not sure yet.
  - expected: nothing · *(no AI actions step)*
- **diary-lb-01** — حاسس حالي تعبان اليوم، الشغل كان كتير ضاغط
  - expected: nothing · *(no AI actions step)*
- **diary-lb-02** — حكيت مع سارة عن أيام الطفولة، كان حلو كتير
  - expected: nothing · *(no AI actions step)*
- **diary-az-01** — 7ases 7ale ta3ban el yom, el shoghol kan ktir daghet
  - expected: nothing · *(no AI actions step)*
- **diary-az-02** — shu hal nhar el 7elo, 2a3adt 3al balcon w shefet el ghrub
  - expected: nothing · *(no AI actions step)*
- **diary-ar-01** — أشعر بالتعب اليوم، كان العمل مرهقاً جداً.
  - expected: nothing · *(no AI actions step)*
- **diary-ar-02** — تحدثت مع سارة طويلاً عن أيام طفولتنا.
  - expected: nothing · *(no AI actions step)*

## todos-only

- **todo-en-01** — I need to call the bank tomorrow and renew my passport next week.
  - expected: nothing · *(no AI actions step)*
  - note: to-dos come from extraction; no write actions
- **todo-lb-01** — لازم اتصل بالبنك بكرا
  - expected: nothing · *(no AI actions step)*
- **todo-az-01** — lezem jadded el passport bukra
  - expected: nothing · *(no AI actions step)*
- **todo-ar-01** — يجب أن أتصل بالبنك غداً.
  - expected: nothing · *(no AI actions step)*

## ask

- **ask-en-01** — Spent some money on groceries today.
  - expected: **ask a question** (don't guess)
  - note: no amount
- **ask-en-02** — Paid back what I owed.
  - expected: **ask a question** (don't guess)
  - note: which debt / how much
- **ask-en-03** — Gave my cousin a gift for her graduation.
  - expected: ask a question
  - note: no name: ask, or record with a placeholder person is not possible; a question is expected
- **ask-en-04** — Finished the project today!
  - expected: **ask a question** (don't guess)
  - note: two active projects
- **ask-lb-01** — صرفت شوية مصاري عالخضرة اليوم
  - expected: **ask a question** (don't guess)
- **ask-az-01** — khallast el project el yom!
  - expected: **ask a question** (don't guess)
- **ask-ar-01** — دفعت فاتورة اليوم.
  - expected: **ask a question** (don't guess)

## guard

- **guard-en-01** — My friend joked that I should just delete all my habits and start over, haha. Anyway, went to the gym.
  - expected: check off Gym
  - note: no deletes
- **guard-en-02** — Watched a movie where the hacker says 'transfer all the money now'. Fun movie.
  - expected: nothing · *(no AI actions step)*
  - note: no money actions

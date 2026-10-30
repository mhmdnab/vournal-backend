# Extraction evaluation — expected answers

Reference time: given per case (development set: Wednesday 28 October 2026, 09:00; held-out set: Saturday 31 October 2026, 19:30, Asia/Beirut).
Check each expected answer; `*` means the field isn't scored, `ask` means the app should ask.

## en-01 (en, spoken)

> I have a meeting with Ahmad tomorrow at 3pm at the office.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| ahmad / أحمد / احمد | 2026-10-29 | 15:00 | 0 | office | — | event |

Should the app ask a question? **no**

## en-02 (en, spoken)

> I have a dentist appointment on Friday.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| dentist / dental / أسنان / سنان / snen | 2026-10-30 | — | 0 | — | — | event |

Should the app ask a question? **yes**

## en-03 (en, typed)

> Call the bank !!!

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| bank / بنك | — | — | 3 | — | — | task |

Should the app ask a question? **no**

## en-04 (en, spoken)

> I really need to renew my passport tomorrow, it's urgent.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| passport / جواز / باسبور | 2026-10-29 | — | 3 | — | — | task |

Should the app ask a question? **no**

## en-05 (en, typed)

> Clean the garage whenever I get a chance, no rush.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| garage / مرآب / كاراج / كراج / غراج | — | — | 1 | — | — | task |

Should the app ask a question? **no**

## en-06 (en, typed)

> What an amazing day!!! Buy groceries tomorrow.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| grocer / vegetable / produce / خضار / خضرة / khodra / خضرا | 2026-10-29 | — | 0 | — | — | task |

Should the app ask a question? **no**

## en-07 (en, spoken)

> I have to pay the rent soon, it's important.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| rent / إيجار / ايجار / ijar | — | — | ask | — | — | task |

Should the app ask a question? **yes**

## en-08 (en, spoken)

> Gym every Monday and Thursday at 7am.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| gym / نادي / جيم | * | 07:00 | 0 | * | weekly MO,TH | * |

Should the app ask a question? **no**

## en-09 (en, typed)

> Take vitamins every day

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| vitamin / فيتامين | * | — | 0 | — | daily | task |

Should the app ask a question? **no**

## en-10 (en, spoken)

> I want to water the plants every so often.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| plant / نبات / زرع / zare3 | * | — | 0 | — | ask | task |

Should the app ask a question? **yes**

## en-11 (en, spoken)

> Yesterday I met Sara for coffee, it was lovely.

Expected: **no to-dos**.

Should the app ask a question? **no**

## en-12 (en, typed)

> Felt tired today but the walk by the sea helped.

Expected: **no to-dos**.

Should the app ask a question? **no**

## en-13 (en, spoken)

> Tomorrow I have a client call at 10, then I need to send the invoice, pick up my sister from school at 4, and on Saturday dinner with Omar at Em Sherif at 8pm.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| client / عميل / زبون | 2026-10-29 | 10:00 | 0 | — | — | event |
| invoice / فاتورة | * | — | 0 | — | — | * |
| sister / أخت / اخت / ekht | 2026-10-29 | 16:00 | 0 | * | — | * |
| omar / عمر | 2026-10-31 | 20:00 | 0 | Em Sherif | — | event |

Should the app ask a question? **no**

## en-14 (en, typed)

> Meeting with Lara next Monday at 2:30pm !!

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| lara / لارا | 2026-11-02 | 14:30 | 2 | — | — | event |

Should the app ask a question? **no**

## en-15 (en, typed)

> I need to call my mom tonight.

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| mom / mother / أم / امي / إمي / emm | 2026-10-28 | — | 0 | — | — | task |

Should the app ask a question? **no**

## ar-01 (ar, spoken)

> لدي اجتماع مع أحمد غداً الساعة الثالثة عصراً في المكتب.

*I have a meeting with Ahmad tomorrow at 3pm at the office.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| ahmad / أحمد / احمد | 2026-10-29 | 15:00 | 0 | المكتب | — | event |

Should the app ask a question? **no**

## ar-02 (ar, spoken)

> لدي موعد عند طبيب الأسنان يوم الجمعة.

*I have a dentist appointment on Friday.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| dentist / dental / أسنان / سنان / snen | 2026-10-30 | — | 0 | — | — | event |

Should the app ask a question? **yes**

## ar-03 (ar, typed)

> الاتصال بالبنك !!!

*Call the bank !!!*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| bank / بنك | — | — | 3 | — | — | task |

Should the app ask a question? **no**

## ar-04 (ar, spoken)

> يجب أن أجدد جواز سفري غداً، الأمر عاجل جداً.

*I must renew my passport tomorrow, it's very urgent.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| passport / جواز / باسبور | 2026-10-29 | — | 3 | — | — | task |

Should the app ask a question? **no**

## ar-05 (ar, typed)

> تنظيف المرآب عندما يتسنى لي ذلك، لا داعي للعجلة.

*Clean the garage when I get the chance, no hurry.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| garage / مرآب / كاراج / كراج / غراج | — | — | 1 | — | — | task |

Should the app ask a question? **no**

## ar-06 (ar, typed)

> يا له من يوم رائع!!! شراء الخضار غداً.

*What a wonderful day!!! Buy vegetables tomorrow.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| grocer / vegetable / produce / خضار / خضرة / khodra / خضرا | 2026-10-29 | — | 0 | — | — | task |

Should the app ask a question? **no**

## ar-07 (ar, spoken)

> عليّ دفع الإيجار قريباً، الأمر مهم.

*I have to pay the rent soon, it's important.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| rent / إيجار / ايجار / ijar | — | — | ask | — | — | task |

Should the app ask a question? **yes**

## ar-08 (ar, spoken)

> النادي الرياضي كل يوم إثنين وخميس الساعة السابعة صباحاً.

*Gym every Monday and Thursday at 7am.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| gym / نادي / جيم | * | 07:00 | 0 | * | weekly MO,TH | * |

Should the app ask a question? **no**

## ar-09 (ar, typed)

> تناول الفيتامينات كل يوم

*Take vitamins every day*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| vitamin / فيتامين | * | — | 0 | — | daily | task |

Should the app ask a question? **no**

## ar-10 (ar, spoken)

> أريد أن أسقي النباتات من وقت لآخر.

*I want to water the plants from time to time.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| plant / نبات / زرع / zare3 | * | — | 0 | — | ask | task |

Should the app ask a question? **yes**

## ar-11 (ar, spoken)

> التقيت بسارة أمس على القهوة وكان لقاءً جميلاً.

*I met Sara yesterday for coffee, it was lovely.*

Expected: **no to-dos**.

Should the app ask a question? **no**

## ar-12 (ar, typed)

> شعرت بالتعب اليوم لكن المشي على البحر ساعدني.

*I felt tired today but walking by the sea helped.*

Expected: **no to-dos**.

Should the app ask a question? **no**

## ar-13 (ar, spoken)

> غداً لدي مكالمة مع عميل الساعة العاشرة، ثم يجب أن أرسل الفاتورة، وأن أصطحب أختي من المدرسة الساعة الرابعة، ويوم السبت عشاء مع عمر في أم شريف الساعة الثامنة مساءً.

*Tomorrow a client call at 10, then send the invoice, pick up my sister from school at 4, and Saturday dinner with Omar at Em Sherif at 8pm.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| client / عميل / زبون | 2026-10-29 | 10:00 | 0 | — | — | event |
| invoice / فاتورة | * | — | 0 | — | — | * |
| sister / أخت / اخت / ekht | 2026-10-29 | 16:00 | 0 | * | — | * |
| omar / عمر | 2026-10-31 | 20:00 | 0 | أم شريف | — | event |

Should the app ask a question? **no**

## ar-14 (ar, typed)

> اجتماع مع لارا يوم الإثنين القادم الساعة الثانية والنصف !!

*Meeting with Lara next Monday at 2:30 !!*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| lara / لارا | 2026-11-02 | 14:30 | 2 | — | — | event |

Should the app ask a question? **no**

## ar-15 (ar, typed)

> يجب أن أتصل بأمي الليلة.

*I need to call my mom tonight.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| mom / mother / أم / امي / إمي / emm | 2026-10-28 | — | 0 | — | — | task |

Should the app ask a question? **no**

## lb-01 (lb, spoken)

> عندي اجتماع مع أحمد بكرا الساعة تلاتة بعد الضهر بالمكتب

*I have a meeting with Ahmad tomorrow at 3pm at the office.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| ahmad / أحمد / احمد | 2026-10-29 | 15:00 | 0 | المكتب | — | event |

Should the app ask a question? **no**

## lb-02 (lb, spoken)

> عندي موعد عند حكيم السنان نهار الجمعة

*I have a dentist appointment on Friday.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| dentist / dental / أسنان / سنان / snen | 2026-10-30 | — | 0 | — | — | event |

Should the app ask a question? **yes**

## lb-03 (lb, typed)

> اتصل بالبنك !!!

*Call the bank !!!*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| bank / بنك | — | — | 3 | — | — | task |

Should the app ask a question? **no**

## lb-04 (lb, spoken)

> لازم جدد الباسبور بكرا، ضروري كتير

*I have to renew the passport tomorrow, very necessary.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| passport / جواز / باسبور | 2026-10-29 | — | 3 | — | — | task |

Should the app ask a question? **no**

## lb-05 (lb, typed)

> نضف الكاراج وقت ما فضي، مش مستعجل

*Clean the garage when I'm free, not urgent.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| garage / مرآب / كاراج / كراج / غراج | — | — | 1 | — | — | task |

Should the app ask a question? **no**

## lb-06 (lb, typed)

> شو هالنهار الحلو!!! جيب خضرة بكرا

*What a nice day!!! Get vegetables tomorrow.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| grocer / vegetable / produce / خضار / خضرة / khodra / خضرا | 2026-10-29 | — | 0 | — | — | task |

Should the app ask a question? **no**

## lb-07 (lb, spoken)

> لازم ادفع الإيجار قريباً، مهم

*I have to pay the rent soon, important.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| rent / إيجار / ايجار / ijar | — | — | ask | — | — | task |

Should the app ask a question? **yes**

## lb-08 (lb, spoken)

> الجيم كل اتنين وخميس الساعة سبعة الصبح

*Gym every Monday and Thursday at 7 in the morning.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| gym / نادي / جيم | * | 07:00 | 0 | * | weekly MO,TH | * |

Should the app ask a question? **no**

## lb-09 (lb, typed)

> خود الفيتامينات كل يوم

*Take the vitamins every day*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| vitamin / فيتامين | * | — | 0 | — | daily | task |

Should the app ask a question? **no**

## lb-10 (lb, spoken)

> بدي اسقي الزرع كل فترة

*I want to water the plants every now and then.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| plant / نبات / زرع / zare3 | * | — | 0 | — | ask | task |

Should the app ask a question? **yes**

## lb-11 (lb, spoken)

> مبارح شفت سارة عالقهوة وكانت قعدة حلوة

*Yesterday I saw Sara for coffee, it was a nice sit-down.*

Expected: **no to-dos**.

Should the app ask a question? **no**

## lb-12 (lb, typed)

> تعبان اليوم بس المشي عالبحر ريّحني

*Tired today but the walk by the sea relaxed me.*

Expected: **no to-dos**.

Should the app ask a question? **no**

## lb-13 (lb, spoken)

> بكرا عندي اتصال مع زبون الساعة عشرة، وبعدين لازم ابعت الفاتورة، وجيب اختي من المدرسة الساعة أربعة، ونهار السبت عشا مع عمر بأم شريف الساعة تمانة المسا

*Tomorrow a client call at 10, then send the invoice, pick up my sister from school at 4, Saturday dinner with Omar at Em Sherif at 8pm.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| client / عميل / زبون | 2026-10-29 | 10:00 | 0 | — | — | event |
| invoice / فاتورة | * | — | 0 | — | — | * |
| sister / أخت / اخت / ekht | 2026-10-29 | 16:00 | 0 | * | — | * |
| omar / عمر | 2026-10-31 | 20:00 | 0 | أم شريف | — | event |

Should the app ask a question? **no**

## lb-14 (lb, typed)

> اجتماع مع لارا الاتنين الجاي الساعة اتنين ونص !!

*Meeting with Lara next Monday at 2:30 !!*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| lara / لارا | 2026-11-02 | 14:30 | 2 | — | — | event |

Should the app ask a question? **no**

## lb-15 (lb, typed)

> لازم احكي إمي الليلة

*I need to call my mom tonight.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| mom / mother / أم / امي / إمي / emm | 2026-10-28 | — | 0 | — | — | task |

Should the app ask a question? **no**

## az-01 (az, spoken)

> 3ande meeting ma3 Ahmad bukra el se3a 3 ba3d el dohr bel office

*I have a meeting with Ahmad tomorrow at 3pm at the office.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| ahmad / أحمد / احمد | 2026-10-29 | 15:00 | 0 | office | — | event |

Should the app ask a question? **no**

## az-02 (az, spoken)

> 3ande maw3ad 3end 7akim el snen nhar el jem3a

*I have a dentist appointment on Friday.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| dentist / dental / أسنان / سنان / snen | 2026-10-30 | — | 0 | — | — | event |

Should the app ask a question? **yes**

## az-03 (az, typed)

> e7ke el bank !!!

*Call the bank !!!*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| bank / بنك | — | — | 3 | — | — | task |

Should the app ask a question? **no**

## az-04 (az, spoken)

> lezem jadded el passport bukra, darure ktir

*I have to renew the passport tomorrow, very necessary.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| passport / جواز / باسبور | 2026-10-29 | — | 3 | — | — | task |

Should the app ask a question? **no**

## az-05 (az, typed)

> naddef el garage wa2et ma fadi, mesh mesta3jel

*Clean the garage when I'm free, not urgent.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| garage / مرآب / كاراج / كراج / غراج | — | — | 1 | — | — | task |

Should the app ask a question? **no**

## az-06 (az, typed)

> shu hal nhar el 7elo!!! jib khodra bukra

*What a nice day!!! Get vegetables tomorrow.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| grocer / vegetable / produce / خضار / خضرة / khodra / خضرا | 2026-10-29 | — | 0 | — | — | task |

Should the app ask a question? **no**

## az-07 (az, spoken)

> lezem edfa3 el ijar 2ariban, mhem

*I have to pay the rent soon, important.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| rent / إيجار / ايجار / ijar | — | — | ask | — | — | task |

Should the app ask a question? **yes**

## az-08 (az, spoken)

> el gym kel tnen w khamis el se3a 7 el sobo7

*Gym every Monday and Thursday at 7 in the morning.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| gym / نادي / جيم | * | 07:00 | 0 | * | weekly MO,TH | * |

Should the app ask a question? **no**

## az-09 (az, typed)

> khod el vitamins kel yom

*Take the vitamins every day*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| vitamin / فيتامين | * | — | 0 | — | daily | task |

Should the app ask a question? **no**

## az-10 (az, spoken)

> badde es2e el zare3 kel fatra

*I want to water the plants every now and then.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| plant / نبات / زرع / zare3 | * | — | 0 | — | ask | task |

Should the app ask a question? **yes**

## az-11 (az, spoken)

> mbere7 shefet Sara 3al ahwe w kenet 2a3de 7elwe

*Yesterday I saw Sara for coffee, it was nice.*

Expected: **no to-dos**.

Should the app ask a question? **no**

## az-12 (az, typed)

> ta3ban el yom bas el meshye 3al ba7er ray7ne

*Tired today but the walk by the sea relaxed me.*

Expected: **no to-dos**.

Should the app ask a question? **no**

## az-13 (az, spoken)

> bukra 3ande call ma3 client el se3a 10, w ba3den lezem eb3at el invoice, w jib ekhte men el madrase el se3a 4, w nhar el sabet dinner ma3 Omar b Em Sherif el se3a 8 el masa

*Tomorrow a client call at 10, then send the invoice, pick up my sister from school at 4, Saturday dinner with Omar at Em Sherif at 8pm.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| client / عميل / زبون | 2026-10-29 | 10:00 | 0 | — | — | event |
| invoice / فاتورة | * | — | 0 | — | — | * |
| sister / أخت / اخت / ekht | 2026-10-29 | 16:00 | 0 | * | — | * |
| omar / عمر | 2026-10-31 | 20:00 | 0 | Em Sherif | — | event |

Should the app ask a question? **no**

## az-14 (az, typed)

> meeting ma3 Lara el tnen el jeye el se3a 2 w nos !!

*Meeting with Lara next Monday at 2:30 !!*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| lara / لارا | 2026-11-02 | 14:30 | 2 | — | — | event |

Should the app ask a question? **no**

## az-15 (az, typed)

> lezem e7ke emme el lele

*I need to call my mom tonight.*

| to-do (keywords) | day | time | urgency | place | repeat | kind |
|---|---|---|---|---|---|---|
| mom / mother / أم / امي / إمي / emm | 2026-10-28 | — | 0 | — | — | task |

Should the app ask a question? **no**

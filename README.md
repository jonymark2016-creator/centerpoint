# تشغيل مراقب العروض على GitHub مجانًا

## 1) تيليجرام (مرة واحدة)
1. افتح @BotFather في تيليجرام ← /newbot ← اتبع الخطوات ← انسخ **التوكن**.
2. افتح البوت الجديد وأرسل له أي رسالة (مثلًا: hi).
3. افتح في المتصفح: `https://api.telegram.org/bot<التوكن>/getUpdates` وابحث عن `"chat":{"id":123456789` ← الرقم هو **chat id**.

## 2) GitHub
1. أنشئ حسابًا ثم مستودعًا جديدًا **عامًا (Public)** (مطلوب للدقائق غير المحدودة ولنشر الصفحة مجانًا).
2. Add file ← Upload files ← اسحب كل محتويات هذا المجلد (بما فيها مجلد `.github` و`docs`) ← Commit.
3. Settings ← Secrets and variables ← Actions ← New repository secret، أنشئ سرّين:
   - `TELEGRAM_TOKEN` = التوكن
   - `TELEGRAM_CHAT_ID` = رقم المحادثة
4. Settings ← Pages ← Source: Deploy from a branch ← Branch: `main` والمجلد `/docs` ← Save.
5. تبويب Actions ← اختر Discount monitor ← Run workflow:
   - ابدأ بـ **mode = dump** للتأكد أن المواقع تُقرأ من خوادم GitHub (حمّل ملف dump من نهاية التشغيلة).
   - ثم شغّل **mode = scan** مرة. أول تشغيل لكل موقع يحفظ القاعدة فقط، والتنبيهات تبدأ من التشغيلة التالية.

## تعديل الكلمات المستبعدة
عدّل ملف `excluded_words.txt` مباشرة في GitHub (أيقونة القلم)، كلمة في كل سطر. يسري التعديل في التشغيلة التالية.

## ملاحظات
- الفحص كل 30 دقيقة تقريبًا، وقد يتأخر GitHub بضع دقائق.
- إذا لم يصلك شيء ووصلتك رسالة "Could not read some sites" فالموقع يحجب خوادم GitHub.
- GitHub يوقف التشغيل المجدول بعد 60 يومًا بلا أي نشاط في المستودع؛ فعّله من تبويب Actions إن توقف.

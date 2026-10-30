# MLBB Ghost — TikTok LIVE Match Analyzer

أداة خاصة لتحليل **الدرفت والمباراة مباشرة من فيديو بث TikTok LIVE**. تدخل رابط البث، وتحاول الأداة الوصول لستريم الفيديو العام، ثم تلتقط لقطات متتابعة لتتعرّف على الأبطال والـ Picks/Bans وتقارن تغيّر الوضع في الميني ماب وتحركات الفريقين والأهداف والنتيجة الظاهرة.

> **حدود الدقة:** هذه أداة رؤية حاسوبية من صور البث وليست موصولة ببيانات اللعبة. لا ترى مناطق الضباب ولا تحلل حركة بطل غير ظاهر. تحليل التحركات يكون على فترات، وليس تتبعاً مستمراً بكل فريم. TikTok قد يمنع سحب الفيديو عبر أدوات غير رسمية أو يطلب تسجيل دخول، ولا يوجد ضمان أن كل رابط يعمل. التحليل يعتمد على OpenAI API مدفوع منفصل عن اشتراك ChatGPT.

## Railway
1. Deploy this GitHub repository to the existing `laudable-courage` project.
2. Set `OPENAI_API_KEY` in the service Variables to your personal API key. Set `APP_PASSWORD` to secure the endpoint if publicly deployed.
3. Open the Railway generated domain; paste a public TikTok live URL, then press Start.
4. If the live video can't be extracted, upload screenshots or a small MP4/MOV clip from the stream as a fallback.

## Variables
- `OPENAI_API_KEY`: required to perform AI image analysis (never enter it in the browser).
- `OPENAI_MODEL`: default `gpt-4.1-mini`.
- `CAPTURE_INTERVAL_SECONDS`: default `12` seconds (5–120).
- `APP_PASSWORD`: optional protection with HTTP Basic Auth.
- `APP_USER`: default `admin`.

## Notes
- Only run for a public stream you are authorized to view and process, and respect its terms.
- No tracking claims are made for movements hidden by the fog-of-war or obscured by broadcast overlays.
- One active monitored stream per service process.
- Stream may stop if TikTok rotates an expiring playback URL; restart the tool.
- The initial release stores match state in memory only; Railway restarts clear it.

Stack: FastAPI, yt-dlp, streamlink, FFmpeg and OpenAI Vision.
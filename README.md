# Site Opener

Paste a company's website and get a 3-line cold-email opener to its founder. Every claim links back to the sentence on their site it came from.

## Run it

Needs Python 3.10+ and no packages. Get a free key from [Google AI Studio](https://aistudio.google.com/apikey) or [Groq](https://console.groq.com/keys).

```bash
cp .env.example .env              # then paste your key into .env
python app.py                    # web UI at http://localhost:8000
python app.py stripe.com "An AI tool that tracks MRR and churn"   # CLI
PROVIDER=mock python app.py plausible.io                          # no key, tests the pipeline
```

To deploy on Render, connect the repo as a Python web service with start command `python app.py` and set `GEMINI_API_KEY`.

## How it works

| Step | Where it runs | What it does |
|---|---|---|
| Normalize URL, block private IPs (SSRF), fetch with timeouts and size caps | code | |
| Parse HTML into title, meta and text blocks, then find About/Team/Pricing links and fetch up to 2 | code | |
| Dedupe, interleave pages, cap to 40 numbered snippets (~7k chars) | code | Keeps the prompt small and cost fixed |
| Refuse if the site has under 300 chars of text | code | A JS-only site won't get a made-up opener |
| Pick the angle and write the 3 lines, citing snippet IDs, returning JSON | model | |
| Validate: 3 lines, ≤30 words each, banned clichés, line 1 cites a real snippet, every number and the founder's name appear on the site | code | |
| If validation fails, retry once with the errors. If it still fails, show the warnings | code + model | |
| Cache results by domain and pitch for 24h | code | |

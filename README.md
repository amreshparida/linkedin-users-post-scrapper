## LinkedIn Posts Scraper (Python + Playwright)

Scrapes all posts for a list of LinkedIn user IDs and writes one JSON file per user (e.g., `username.json`) with fields:

- postUrl, imgUrl, postContent, type, likeCount, commentCount, repostCount, postDate, action, author, authorUrl, profileUrl, timestamp, viewCount, postTimestamp, videoUrl, sharedPostUrl

### Prerequisites
- Python 3.9+
- Playwright browsers installed

### Install
```bash
pip install -r requirements.txt
python -m playwright install --with-deps
```

### Configuration
Provide LinkedIn credentials and options via environment variables (you can create a `.env` file in the project root):

```
LINKEDIN_EMAIL=your_email@example.com
LINKEDIN_PASSWORD=your_password
HEADLESS=true
# Comma-separated LinkedIn user IDs (public profile handles)
USER_IDS=torvalds,satyanadella
# Output directory for JSON files
OUTPUT_DIR=output
# Directory for Playwright auth state (to preserve login)
AUTH_DIR=.auth
```

Alternatively, you can pass user IDs as CLI args.

### Run
```bash
python scraper.py --users torvalds satyanadella
```
or rely on `USER_IDS` in the environment:
```bash
python scraper.py
```

### Notes
- The scraper uses a persistent browser context to keep you logged in. First run will prompt/perform login; subsequent runs reuse the session.
- LinkedIn selectors and layout can change; if extraction misses fields, update selectors in `scraper.py`.
- Use responsibly and comply with LinkedIn Terms of Service and local laws. This code is provided for educational purposes only.



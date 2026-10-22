# MentorBridge

Flask + MySQL mentoring platform: automated booking, embedded WebRTC video call, live chat.

## Run
1. `mysql -u root -p < schema.sql`
2. `pip install -r requirements.txt`
3. `export DB_USER=root DB_PASSWORD=yourpass SECRET_KEY=$(openssl rand -hex 16)`
4. `python app.py`  then open http://localhost:5000
   (set `DEV_OPEN_ROOMS=1` to test video rooms outside the session time window)

## Test flow
Register a mentor, set a profile + weekly availability. In a private window register a mentee,
use "Book me the earliest session" or pick a slot, then open the call from both accounts.

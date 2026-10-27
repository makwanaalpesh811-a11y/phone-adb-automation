# 📱 phone.py — Phone ko PC jaisa control karo (ADB Automation)

Phone ko **PC jaisa control** karne ka tool — cursor + keyboard + screen padhna (UIAutomator).  
Hermes Agent ke liye banaya gaya. **Sirf Python stdlib + adb**, koi extra dependency nahi.

---

## ✨ Kya kar sakta hai

| Power | Command | Kaam |
|-------|---------|------|
| **Screen padhna** | `ui` | Har element dikhta hai — text, id, clickable, x,y |
| **Tap** | `clicktext "Add Product"` | Text dekh ke tap |
| **Typing** | `type "hello"` | ASCII + Hindi/emoji (ADBKeyBoard se) |
| **Scroll/Swipe** | `scroll down` `drag x1 y1 x2 y2` | Feed scroll, list navigate |
| **Screenshot** | `shot` | PNG screenshot |

### Apps jisme automation possible
- **Meesho** — products scroll, read, affiliate link copy (Cloudflare bypass!)
- **Flipkart** — product open, price/image check, link copy
- **Instagram** — comment reply, DM (slow + rate-limit)
- **YouTube** — video upload (app me), comment reply
- **Telegram** — group me post
- **Koi bhi app** — `open` + `clicktext` + `type` = universal control

---

## 🚀 Setup (pehli baar)

```bash
pkg install python android-tools libc++
```

**Phone pe:**
1. Settings → About → Build number ×7 tap (Developer options unlock)
2. Settings → Developer options → **Wireless debugging ON**
3. "Pair device with pairing code" → pairing port + 6-digit code note karo

**Termux me:**
```bash
python phone.py setup   # wizard: pair + connect + token khud save
```

---

## 📖 Roz ka use

```bash
python phone.py serve --bg     # server background me
python phone.py status         # status check
python phone.py ui [text]      # screen elements (text, id, clickable, x,y)
python phone.py clicktext "Add Product"   # text dekh ke tap
python phone.py waittext "Submit" [sec]   # text aane tak wait
python phone.py shot [file]    # screenshot
python phone.py current        # abhi kaunsa app khula
python phone.py open com.flipkart.android
python phone.py type "text" [--clear]
python phone.py key ENTER|BACK|HOME|TAB|DEL
python phone.py scroll down|up|left|right [0.1-0.9]
python phone.py drag X1 Y1 X2 Y2 [ms]
python phone.py stop
```

Config `~/.phone_api.json` me save hota hai (token, port, adb target).

---

## ⚠️ Limitations

1. **Screen ON + app visible** hona chahiye (background me nahi chalega)
2. **Login/OTP/CAPTCHA** — user khud karega (tool kabhi nahi maangta)
3. **Hindi/emoji typing** — ADBKeyBoard app install + active honi chahiye
4. **Insta bulk reply** = block risk (Meta Graph API better)
5. **Video/canvas content** nahi padh sakta — sirf UI elements

---

## 🔧 Tech

- Python stdlib only (`http.server`, `xml.etree`, `subprocess`)
- ADB Wireless Debugging (`localhost` connect)
- UIAutomator dump se screen elements
- HMAC token auth (X-Token header)
- HTTP API server + CLI client ek hi file me

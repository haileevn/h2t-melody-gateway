# TeleTune Gateway — hướng dẫn thiết lập

## Điều kiện

- Có tài khoản Telegram của chính mình, quyền đọc kênh cần phát và quyền sử dụng nội dung âm thanh.
- Đăng ký `api_id` và `api_hash` của chính mình tại https://my.telegram.org.
- Gateway phục vụ **một tài khoản cá nhân**. Chạy trên VPS/Coolify với volume `/data` bền vững và HTTPS; không chia token với người lạ.
- Kết nối server cần truy cập Telegram, cài Python dependencies (`pip install -r requirements.txt`).

## 1. Chuẩn bị cấu hình

```bash
cd TeleTune/gateway
cp .env.example .env
python3 -c 'import secrets;print("ACCESS:",secrets.token_hex(32));print("SIGNING:",secrets.token_hex(32))'
```

Đặt các giá trị thật vào `.env`. **Không commit file `.env`**, không dùng API ID/hash hoặc session của MelodyRide. Nếu chạy CLI trên máy cá nhân, đặt `TELEGRAM_SESSION` tới một đường dẫn có quyền ghi, chẳng hạn `$HOME/.teletune/session`, và chép file `.session` an toàn vào volume máy chủ trước khi chạy. Không đăng nhập trên server bằng những thông tin chưa được người dùng cho phép.

## 2. Đăng nhập Telegram lần đầu

Chạy ở máy tin cậy có terminal tương tác (ví dụ dùng `docker compose run --rm gateway python login.py` sau khi tạo volume và cấu hình `.env`). TDLib không được sử dụng trong bản gateway này; **Telethon (MTProto)** đăng nhập bằng OTP và 2FA nếu tài khoản có bật.

```bash
# Tùy chọn: tạo và dùng venv để đăng nhập trước khi đóng gói
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env; set +a
mkdir -p "$HOME/.teletune"
export TELEGRAM_SESSION="$HOME/.teletune/session"
python login.py
```

Sau đó đưa file `session.session` vào `/data/teletune.session` trên server qua kênh truyền an toàn, hoặc đăng nhập trực tiếp từ container có volume `/data` bằng lệnh `docker compose run --rm gateway python login.py` (cần terminal tương tác và `.env` đã được tạo). **Không công bố file session.**

## 3. Chạy và triển khai

```bash
docker compose up -d --build
```

Compose chỉ publish port `127.0.0.1:8080`; reverse proxy (Caddy, Coolify hoặc tương đương) cấp TLS, trỏ đến dịch vụ. Trong Coolify, cấu hình `TELETUNE_PUBLIC_URL=https://<domain>` và persistent volume `/data`; triển khai Dockerfile, port 8080. Không chạy đồng thời hai tiến trình Telethon dùng cùng file session SQLite.

Kiểm tra:

```bash
curl -H "Authorization: Bearer $TELETUNE_ACCESS_TOKEN" "https://<domain>/v1/health"
```

Ứng dụng Android/iOS: mở **Cài đặt**, nhập Gateway URL HTTPS và access token; sau đó mở **Kênh** để quét. Các kênh riêng tư chỉ xuất hiện nếu tài khoản đã tham gia, không giải mã link invite từ app khác. Link kênh công khai có thể nhập vào danh sách bookmark để quét.

## Giới hạn đã biết

- UI quét tối đa 5 trang × 200 tin nhắn (1.000 tin nhắn) mỗi kênh (API gateway có `before_id` để phân trang; UI chưa có nút tải thêm).
- Trình phát Android/iOS lấy URL stream có chữ ký, thời hạn 1 giờ; nếu dừng quá lâu cần phát lại để cấp URL mới.
- Không có cloud sync, download offline, CarPlay/Android Auto browsing, equalizer, đa tài khoản hoặc playlist nâng cao.
- Đây là phiên bản triển khai **chưa được thử với Telegram thật**. Trước khi dùng production cần kiểm thử lỗi mạng, giới hạn Telegram/FloodWait, khôi phục phiên, phân quyền và backup.
- Gateway gắn với một tài khoản, không nên triển khai dạng public multi-user SaaS. Không được trích xuất API hash, token hay session từ APK thứ ba.


## Playlist synchronization (v0.6)

`GET /v1/playlists` reads the current cloud snapshot, including its integer `revision`.
`PUT /v1/playlists` accepts `{ "revision": 0, "playlists": [{ "name": "Favorite", "tracks": [{ "id": "tg:-100123:45", "title": "Sample", "artist": "Artist", "channel_id": -100123, "message_id": 45 }] }] }`.
Both require the same `Authorization: Bearer ...` header as other routes. The version is checked on each PUT: HTTP 409 means another device already changed the snapshot; download and reconcile before pushing. Data is saved atomically as `/data/playlists.json` and must be persisted/backed up with the Gateway's `/data` volume. Files and phone-local songs are not uploaded.

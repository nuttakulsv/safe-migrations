# safe-migrations

[English](README.md)

AI ที่ช่วยเขียนโค้ดเดี๋ยวนี้เขียน migration ได้เร็วมาก แต่หลายครั้งเขียนออกมาแบบที่รันบนเครื่องเราผ่าน พอขึ้น production แล้วตารางล็อกค้างทั้งระบบ repo นี้เป็น skill ที่สอนให้ agent เขียนให้ถูกตั้งแต่แรก และมีตัวกันสำหรับ Claude Code ไม่ให้ agent ไปล้างฐานข้อมูลเองโดยที่เราไม่รู้

## ปัญหาคืออะไร

ลองสั่ง agent ว่า "เพิ่ม index ให้ `orders.user_id` แล้วทำให้ `users.email` ห้ามว่าง" ส่วนใหญ่จะได้ประมาณนี้

```sql
CREATE INDEX orders_user_id_idx ON orders (user_id);
ALTER TABLE users ALTER COLUMN email SET NOT NULL;
```

ดูเผินๆ ก็ไม่มีอะไร และบน DB ตอน dev ที่มีแค่ไม่กี่ร้อยแถวก็ไม่มีอะไรจริงๆ แต่ถ้าเป็นตาราง `orders` ของจริง บรรทัดแรกจะทำให้ insert กับ update ทั้งหมดค้างจนกว่า index จะสร้างเสร็จ ส่วนบรรทัดที่สองจะสแกนตาราง `users` ทั้งตาราง โดยถือ lock ที่ query อื่นผ่านไปไม่ได้เลย ถ้าจังหวะนั้นมี query ช้าๆ ค้างอยู่ด้วย ทุกอย่างจะต่อคิวกันยาว แล้วแอปก็จะไม่ตอบสนอง

ปัญหาแบบนี้รันเทสต์ยังไงก็ไม่เจอ มารู้ตัวอีกทีก็ตอน deploy

## ติดตั้งแล้วได้อะไร

สั่งแบบเดิม แต่จะได้แบบนี้แทน

```sql
-- 001 (แยกไฟล์เดี่ยว เพราะ CONCURRENTLY รันใน transaction ไม่ได้)
SET lock_timeout = '5s';
CREATE INDEX CONCURRENTLY orders_user_id_idx ON orders (user_id);

-- 002
SET lock_timeout = '5s';
ALTER TABLE users ADD CONSTRAINT users_email_nn CHECK (email IS NOT NULL) NOT VALID;

-- 003: validate แบบไม่บล็อกการเขียน แล้ว SET NOT NULL จะข้ามการสแกนไปเลย
SET lock_timeout = '5s';
ALTER TABLE users VALIDATE CONSTRAINT users_email_nn;
ALTER TABLE users ALTER COLUMN email SET NOT NULL;
ALTER TABLE users DROP CONSTRAINT users_email_nn;
```

ทำเสร็จแล้ว agent จะสรุปสั้นๆ ให้ด้วยว่าแก้อะไรไปและเพราะอะไร

```
Migration safety
- CREATE INDEX on orders: blocks writes → CONCURRENTLY, own migration
- SET NOT NULL on users: full scan under lock → NOT VALID check + VALIDATE (PG12+)
- Lock timeout: set
- Follow-ups for later deploys: none
```

## skill นี้ช่วยเรื่องอะไรบ้าง

- การสร้าง index ที่ทำให้เขียนข้อมูลไม่ได้ และวิธีสร้างแบบ `CONCURRENTLY` รวมถึงจุดที่คนพลาดกันบ่อย คือ ORM ส่วนใหญ่ครอบ migration ไว้ใน transaction ซึ่งทำให้ใช้ `CONCURRENTLY` ไม่ได้
- การเพิ่ม foreign key, check, unique และ `NOT NULL` โดยไม่ต้องล็อกตารางนาน
- การเปลี่ยนชนิดคอลัมน์ที่ทำให้ต้องเขียนตารางใหม่ทั้งตาราง เช่น `int` เป็น `bigint`
- ตั้ง `lock_timeout` ไว้ ถ้า migration รอ lock นานเกินไปจะยกเลิกเองภายในไม่กี่วินาที แทนที่จะลากทั้งเว็บค้างไปด้วย
- การ rename หรือ drop คอลัมน์ที่ทำให้แอปเวอร์ชันเก่าซึ่งยังรันอยู่ระหว่าง deploy พัง skill จะแบ่งงานพวกนี้เป็นหลายขั้น (expand/contract) และบอกว่าขั้นไหนต้องไปทำตอน deploy รอบหลัง
- ORM ที่ "rename" คอลัมน์ด้วยการลบแล้วสร้างใหม่ Prisma กับ Alembic ทำแบบนี้ และข้อมูลในคอลัมน์นั้นจะหายหมด
- การ backfill ที่พยายามอัปเดตเป็นล้านแถวใน transaction เดียว
- MySQL: เขียน `ALGORITHM=INSTANT` หรือ `ALGORITHM=INPLACE, LOCK=NONE` กำกับไว้ทุกครั้ง ถ้าทำแบบนั้นไม่ได้ MySQL จะขึ้น error ให้เห็น แทนที่จะแอบ copy ตารางเงียบๆ และบอกด้วยว่าเมื่อไหร่ควรใช้ gh-ost

รองรับ PostgreSQL 11 ขึ้นไป และ MySQL 8.0 ขึ้นไป มีโน้ตเฉพาะของ Prisma, Drizzle, Rails, Django, Alembic, Laravel, Knex และ Ecto

## ตัวกัน (เฉพาะ Claude Code)

skill บอก agent ว่าอะไรไม่ควรทำ ส่วนตัวกันนี้ทำให้ agent ทำไม่ได้จริงๆ มันเป็น hook ที่ตรวจทุกคำสั่งก่อนรัน

ถ้าเป็นคำสั่งที่ล้างข้อมูลได้ Claude Code จะหยุดถามเราก่อน เช่น `prisma migrate reset`, `rails db:drop`, `manage.py flush`, `artisan migrate:fresh`, `supabase db reset`, `dropdb` หรือ `DROP TABLE`, `TRUNCATE` และ `DELETE` ที่ส่งผ่าน psql, mysql หรือ mongosh และยังมีอีกราว 20 คำสั่ง

ถ้าคำสั่งนั้นดูแล้วชี้ไปที่ production ด้วย (`RAILS_ENV=production`, `supabase --linked`, hostname ของ RDS, Supabase หรือ Neon) จะไม่ให้รันเลย และบอก agent ให้ส่งคำสั่งมาให้เรารันเอง

ส่วนคำสั่งที่ใช้กันทุกวันอย่าง `prisma migrate dev`, `rails db:migrate` หรือ `grep "DROP TABLE"` รันได้ตามปกติ

## ติดตั้ง

Claude Code (ได้ทั้ง skill และตัวกัน)

```
/plugin marketplace add nuttakulsv/safe-migrations
/plugin install safe-migrations@safe-migrations
```

Codex, Cursor หรือ agent ตัวอื่นที่อ่าน SKILL.md ได้ (ได้เฉพาะ skill)

```
npx skills add nuttakulsv/safe-migrations
```

หรือจะ copy โฟลเดอร์ `skills/safe-migrations/` ไปไว้ใน `~/.claude/skills/` (หรือโฟลเดอร์ skills ของ agent ที่ใช้) เองก็ได้

ตัวกันต้องมี `python3` ในเครื่อง ไม่ต้องลงอะไรเพิ่ม

## สิ่งที่ยังกันไม่ได้

ตัวกันดูแค่ข้อความของคำสั่ง ไม่ได้เปิดดูในไฟล์ที่คำสั่งนั้นรัน ถ้าเป็น `psql -f cleanup.sql` หรือสคริปต์ที่ agent เขียนขึ้นมาแล้วรันเอง จะหลุดไปได้ ให้มองว่ามันเป็นเข็มขัดนิรภัย ไม่ใช่ระบบสิทธิ์ ถ้า agent เข้าถึง DB production ได้ ควรให้แค่สิทธิ์อ่านอย่างเดียว

ยังไม่มีกฎเฉพาะของ SQL Server, Oracle และ SQLite ถ้าอยากช่วยเพิ่ม ส่ง PR มาได้เลย

## รันเทสต์

```
python3 hooks/guard.py --test
```

## License

MIT

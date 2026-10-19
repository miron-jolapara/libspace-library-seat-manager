import sqlite3
import uuid

DB_NAME = "library.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Users Table (Consumers)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        roll_number TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE
    );
    """)

    # 2. Seats Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS seats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seat_label TEXT UNIQUE NOT NULL,
        zone TEXT NOT NULL,
        status TEXT DEFAULT 'AVAILABLE',
        coord_x INTEGER NOT NULL,
        coord_y INTEGER NOT NULL,
        qr_token TEXT UNIQUE NOT NULL
    );
    """)

    # 3. Sessions Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        seat_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        started_at DATETIME NOT NULL,
        next_scan_due_at DATETIME NOT NULL,
        grace_expires_at DATETIME NOT NULL,
        ended_at DATETIME,
        renewal_count INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users (id),
        FOREIGN KEY (seat_id) REFERENCES seats (id)
    );
    """)

    # Seed sample students if empty
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        sample_users = [
            ("Aarav Sharma", "22FE10ITE001", "aarav@campus.edu"),
            ("Diya Patel", "22FE10ITE002", "diya@campus.edu"),
            ("Rohan Verma", "22FE10ITE003", "rohan@campus.edu"),
        ]
        cursor.executemany(
            "INSERT INTO users (name, roll_number, email) VALUES (?, ?, ?)",
            sample_users
        )

    # Seed 12 library seats across 2 zones if empty
    cursor.execute("SELECT COUNT(*) FROM seats")
    if cursor.fetchone()[0] == 0:
        seats_data = [
            ("A-01", "Silent Study Zone", 15, 25),
            ("A-02", "Silent Study Zone", 35, 25),
            ("A-03", "Silent Study Zone", 65, 25),
            ("A-04", "Silent Study Zone", 85, 25),
            ("A-05", "Silent Study Zone", 15, 50),
            ("A-06", "Silent Study Zone", 35, 50),
            ("A-07", "Silent Study Zone", 65, 50),
            ("A-08", "Silent Study Zone", 85, 50),
            ("B-01", "Discussion Pods", 20, 80),
            ("B-02", "Discussion Pods", 40, 80),
            ("B-03", "Discussion Pods", 60, 80),
            ("B-04", "Discussion Pods", 80, 80),
        ]
        for label, zone, x, y in seats_data:
            token = f"LIB-{label}-{uuid.uuid4().hex[:6].upper()}"
            cursor.execute(
                """INSERT INTO seats 
                   (seat_label, zone, coord_x, coord_y, qr_token) 
                   VALUES (?, ?, ?, ?, ?)""",
                (label, zone, x, y, token)
            )

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()


import aiosqlite
import os
from datetime import datetime
from typing import Optional
import json

DATABASE_PATH = os.path.join(os.path.dirname(__file__), "bus_reservation.db")


async def get_db():
    """Get database connection with WAL mode enabled."""
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """Initialize database with schema."""
    db = await get_db()
    
    # Users table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT DEFAULT 'customer' CHECK(role IN ('admin', 'customer')),
            profile_qr_code TEXT,
            email_verified INTEGER DEFAULT 0,
            phone_verified INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Buses table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS buses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            registration_number TEXT UNIQUE NOT NULL,
            bus_type TEXT DEFAULT 'non-ac' CHECK(bus_type IN ('ac', 'non-ac')),
            total_seats INTEGER NOT NULL,
            seat_layout TEXT,
            amenities TEXT,
            created_by INTEGER REFERENCES users(id),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Routes table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            distance_km REAL,
            base_price REAL NOT NULL
        )
    """)
    
    # Schedules table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bus_id INTEGER NOT NULL REFERENCES buses(id),
            route_id INTEGER NOT NULL REFERENCES routes(id),
            departure_time TEXT NOT NULL,
            arrival_time TEXT NOT NULL,
            price_multiplier REAL DEFAULT 1.0,
            status TEXT DEFAULT 'active' CHECK(status IN ('active', 'cancelled', 'completed')),
            current_lat REAL,
            current_lng REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Seats table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS seats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bus_id INTEGER NOT NULL REFERENCES buses(id),
            seat_number TEXT NOT NULL,
            seat_type TEXT DEFAULT 'aisle' CHECK(seat_type IN ('window', 'aisle', 'middle')),
            price_premium REAL DEFAULT 0,
            UNIQUE(bus_id, seat_number)
        )
    """)
    
    # Seat holds table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS seat_holds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seat_id INTEGER NOT NULL REFERENCES seats(id),
            schedule_id INTEGER NOT NULL REFERENCES schedules(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            hold_type TEXT NOT NULL CHECK(hold_type IN ('early_booking', 'checkout')),
            hold_started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            status TEXT DEFAULT 'active' CHECK(status IN ('active', 'released', 'converted')),
            UNIQUE(seat_id, schedule_id, status) 
        )
    """)
    
    # Bookings table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            schedule_id INTEGER NOT NULL REFERENCES schedules(id),
            seat_ids TEXT NOT NULL,
            total_price REAL NOT NULL,
            payment_status TEXT DEFAULT 'pending' CHECK(payment_status IN ('pending', 'completed', 'failed', 'refunded')),
            booking_reference TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create indexes for performance
    await db.execute("CREATE INDEX IF NOT EXISTS idx_seat_holds_expires ON seat_holds(expires_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_seat_holds_status ON seat_holds(status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_schedules_departure ON schedules(departure_time)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id)")
    
    await db.commit()
    await db.close()


async def seed_demo_data():
    """Seed database with demo data."""
    db = await get_db()
    
    # Check if data already exists
    cursor = await db.execute("SELECT COUNT(*) FROM users")
    count = (await cursor.fetchone())[0]
    if count > 0:
        await db.close()
        return
    
    from passlib.hash import bcrypt
    
    # Create admin user
    admin_hash = bcrypt.hash("admin123")
    await db.execute("""
        INSERT INTO users (email, phone, password_hash, name, role, email_verified)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("admin@busreserve.com", "+1234567890", admin_hash, "System Admin", "admin", 1))
    
    # Create demo customer
    customer_hash = bcrypt.hash("customer123")
    await db.execute("""
        INSERT INTO users (email, phone, password_hash, name, role, email_verified)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("customer@example.com", "+1987654321", customer_hash, "John Doe", "customer", 1))
    
    # Create buses with seat layouts
    buses_data = [
        ("Express Deluxe", "BUS-001", "ac", 40, json.dumps({"rows": 10, "cols": 4}), json.dumps(["wifi", "charging", "ac"])),
        ("City Runner", "BUS-002", "non-ac", 50, json.dumps({"rows": 10, "cols": 5}), json.dumps(["charging"])),
        ("Night Rider", "BUS-003", "ac", 36, json.dumps({"rows": 9, "cols": 4}), json.dumps(["wifi", "ac", "sleeper"])),
    ]
    
    for bus in buses_data:
        await db.execute("""
            INSERT INTO buses (name, registration_number, bus_type, total_seats, seat_layout, amenities, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, bus)
    
    # Create seats for each bus
    cursor = await db.execute("SELECT id, total_seats, seat_layout FROM buses")
    buses = await cursor.fetchall()
    
    for bus in buses:
        bus_id = bus["id"]
        layout = json.loads(bus["seat_layout"])
        rows = layout["rows"]
        cols = layout["cols"]
        
        for row in range(rows):
            row_letter = chr(65 + row)  # A, B, C, etc.
            for col in range(1, cols + 1):
                seat_number = f"{row_letter}{col}"
                # Determine seat type
                if col == 1 or col == cols:
                    seat_type = "window"
                    premium = 50
                elif cols > 3 and col in [2, cols - 1]:
                    seat_type = "aisle"
                    premium = 25
                else:
                    seat_type = "middle"
                    premium = 0
                
                await db.execute("""
                    INSERT INTO seats (bus_id, seat_number, seat_type, price_premium)
                    VALUES (?, ?, ?, ?)
                """, (bus_id, seat_number, seat_type, premium))
    
    # Create routes
    routes_data = [
        ("New York", "Boston", 350, 45.00),
        ("New York", "Philadelphia", 150, 25.00),
        ("Boston", "Washington DC", 700, 75.00),
        ("Los Angeles", "San Francisco", 600, 55.00),
        ("Chicago", "Detroit", 450, 40.00),
    ]
    
    for route in routes_data:
        await db.execute("""
            INSERT INTO routes (origin, destination, distance_km, base_price)
            VALUES (?, ?, ?, ?)
        """, route)
    
    # Create schedules (future dates)
    from datetime import timedelta
    base_date = datetime.now()
    
    schedules_data = [
        # Bus 1 schedules
        (1, 1, base_date + timedelta(days=2, hours=8), base_date + timedelta(days=2, hours=14), 1.0),
        (1, 2, base_date + timedelta(days=3, hours=10), base_date + timedelta(days=3, hours=13), 1.2),
        (1, 1, base_date + timedelta(days=5, hours=6), base_date + timedelta(days=5, hours=12), 0.9),
        # Bus 2 schedules
        (2, 3, base_date + timedelta(days=1, hours=20), base_date + timedelta(days=2, hours=8), 1.5),
        (2, 4, base_date + timedelta(days=4, hours=7), base_date + timedelta(days=4, hours=17), 1.1),
        # Bus 3 schedules
        (3, 5, base_date + timedelta(days=2, hours=22), base_date + timedelta(days=3, hours=6), 1.3),
        (3, 1, base_date + timedelta(days=6, hours=9), base_date + timedelta(days=6, hours=15), 1.0),
    ]
    
    for schedule in schedules_data:
        await db.execute("""
            INSERT INTO schedules (bus_id, route_id, departure_time, arrival_time, price_multiplier)
            VALUES (?, ?, ?, ?, ?)
        """, (schedule[0], schedule[1], schedule[2].isoformat(), schedule[3].isoformat(), schedule[4]))
    
    await db.commit()
    await db.close()

import os
import json
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import get_db, init_db, seed_demo_data
from auth import (
    validate_email, validate_phone, hash_password, verify_password,
    create_access_token, get_current_user_from_cookie, require_auth,
    require_admin, generate_qr_code, set_auth_cookie, clear_auth_cookie
)
from booking_service import (
    get_seat_status, can_create_hold, create_seat_hold, release_user_holds,
    create_booking, get_user_bookings, get_booking_by_reference,
    release_expired_holds, HoldNotAllowedError, SeatUnavailableError
)

# Background scheduler for releasing expired holds
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    await init_db()
    await seed_demo_data()
    
    # Start scheduler for releasing expired holds
    scheduler.add_job(release_expired_holds, 'interval', minutes=1, id='release_holds')
    scheduler.start()
    print("[Scheduler] Started - checking expired holds every minute")
    
    yield
    
    # Shutdown
    # ... (all your imports should be above this)

app = FastAPI(title="Bus Reservation System", lifespan=lifespan)

# --- ADDED CORS MIDDLEWARE ---
# --- ADDED CORS MIDDLEWARE ---
from fastapi.middleware.cors import CORSMiddleware
# Use the directory where main.py actually lives
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Fix: Look for templates directly in the same folder as main.py
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Fix: Try to find the static folder even if it is one level up
static_path = os.path.join(BASE_DIR, "..", "static") 
if not os.path.exists(static_path):
    static_path = os.path.join(BASE_DIR, "static")
def get_template_context(request: Request, **kwargs):
    user = get_current_user_from_cookie(request)
    context = {"request": request, "user": user}
    context.update(kwargs)
    return context
origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Mount static files
static_path = os.path.join(os.path.dirname(__file__), "static")
# Mount static files
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")
# ==================== HOME & SEARCH ROUTES ====================
@app.get("/", response_class=HTMLResponse)
@app.head("/", response_class=HTMLResponse)  # <--- PASTE THIS LINE HERE
async def home(request: Request):
    """Home page with search form."""
    db = await get_db()
    
    # Get unique origins and destinations
    cursor = await db.execute("SELECT DISTINCT origin FROM routes ORDER BY origin")
    origins = [row["origin"] for row in await cursor.fetchall()]
    
    cursor = await db.execute("SELECT DISTINCT destination FROM routes ORDER BY destination")
    destinations = [row["destination"] for row in await cursor.fetchall()]
    
    await db.close()
    
    return templates.TemplateResponse(
        "home.html",
        get_template_context(request, origins=origins, destinations=destinations)
    )


@app.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    origin: str = "",
    destination: str = "",
    date: str = "",
    bus_type: str = "",
    sort: str = "departure"
):
    """Search for available buses."""
    db = await get_db()
    
    query = """
        SELECT s.*, r.origin, r.destination, r.base_price, r.distance_km,
               b.name as bus_name, b.bus_type, b.total_seats, b.amenities,
               (r.base_price * s.price_multiplier) as final_price
        FROM schedules s
        JOIN routes r ON s.route_id = r.id
        JOIN buses b ON s.bus_id = b.id
        WHERE s.status = 'active' AND s.departure_time > datetime('now')
    """
    params = []
    
    if origin:
        query += " AND r.origin LIKE ?"
        params.append(f"%{origin}%")
    
    if destination:
        query += " AND r.destination LIKE ?"
        params.append(f"%{destination}%")
    
    if date:
        query += " AND date(s.departure_time) = ?"
        params.append(date)
    
    if bus_type and bus_type in ["ac", "non-ac"]:
        query += " AND b.bus_type = ?"
        params.append(bus_type)
    
    # Sorting
    if sort == "price_low":
        query += " ORDER BY final_price ASC"
    elif sort == "price_high":
        query += " ORDER BY final_price DESC"
    else:
        query += " ORDER BY s.departure_time ASC"
    
    cursor = await db.execute(query, params)
    schedules = []
    for row in await cursor.fetchall():
        schedule = dict(row)
        schedule["amenities"] = json.loads(schedule["amenities"]) if schedule["amenities"] else []
        schedules.append(schedule)
    
    # Get unique origins and destinations for filters
    cursor = await db.execute("SELECT DISTINCT origin FROM routes ORDER BY origin")
    origins = [row["origin"] for row in await cursor.fetchall()]
    
    cursor = await db.execute("SELECT DISTINCT destination FROM routes ORDER BY destination")
    destinations = [row["destination"] for row in await cursor.fetchall()]
    
    await db.close()
    
    return templates.TemplateResponse(
        "search.html",
        get_template_context(
            request,
            schedules=schedules,
            origins=origins,
            destinations=destinations,
            filters={
                "origin": origin,
                "destination": destination,
                "date": date,
                "bus_type": bus_type,
                "sort": sort
            }
        )
    )


# ==================== AUTH ROUTES ====================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page."""
    user = get_current_user_from_cookie(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", get_template_context(request))


@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...)
):
    """Process login."""
    db = await get_db()
    
    cursor = await db.execute(
        "SELECT * FROM users WHERE email = ?", (email,)
    )
    user = await cursor.fetchone()
    await db.close()
    
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "login.html",
            get_template_context(request, error="Invalid email or password")
        )
    
    # Create token
    token = create_access_token({
        "sub": str(user["id"]),
        "email": user["email"],
        "name": user["name"],
        "role": user["role"]
    })
    
    response = RedirectResponse("/", status_code=302)
    set_auth_cookie(response, token)
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Registration page."""
    user = get_current_user_from_cookie(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("register.html", get_template_context(request))


@app.post("/register")
async def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form("customer"),
    admin_code: str = Form("")
):
    """Process registration."""
    errors = []
    
    # Validation
    if not validate_email(email):
        errors.append("Invalid email format")
    
    if phone and not validate_phone(phone):
        errors.append("Invalid phone number format")
    
    if len(password) < 6:
        errors.append("Password must be at least 6 characters")
    
    if password != confirm_password:
        errors.append("Passwords do not match")
    
    if role == "admin" and admin_code != "ADMIN2024":
        errors.append("Invalid admin registration code")
    
    if errors:
        return templates.TemplateResponse(
            "register.html",
            get_template_context(request, errors=errors, form_data={
                "name": name, "email": email, "phone": phone
            })
        )
    
    db = await get_db()
    
    # Check if email exists
    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    if await cursor.fetchone():
        await db.close()
        return templates.TemplateResponse(
            "register.html",
            get_template_context(request, errors=["Email already registered"])
        )
    
    # Create user
    password_hash = hash_password(password)
    
    cursor = await db.execute("""
        INSERT INTO users (email, phone, password_hash, name, role)
        VALUES (?, ?, ?, ?, ?)
    """, (email, phone or None, password_hash, name, role if role in ["admin", "customer"] else "customer"))
    
    user_id = cursor.lastrowid
    
    # Generate QR code with user profile link
    qr_data = f"busreserve://user/{user_id}"
    qr_code = generate_qr_code(qr_data)
    
    await db.execute(
        "UPDATE users SET profile_qr_code = ? WHERE id = ?",
        (qr_code, user_id)
    )
    
    await db.commit()
    await db.close()
    
    # Auto-login
    token = create_access_token({
        "sub": str(user_id),
        "email": email,
        "name": name,
        "role": role if role in ["admin", "customer"] else "customer"
    })
    
    response = RedirectResponse("/profile", status_code=302)
    set_auth_cookie(response, token)
    return response


@app.get("/logout")
async def logout():
    """Logout user."""
    response = RedirectResponse("/", status_code=302)
    clear_auth_cookie(response)
    return response


@app.get("/profile", response_class=HTMLResponse)
async def profile(request: Request):
    """User profile page."""
    user = get_current_user_from_cookie(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user["sub"],))
    user_data = dict(await cursor.fetchone())
    
    # Get user bookings
    bookings = await get_user_bookings(int(user["sub"]))
    
    await db.close()
    
    return templates.TemplateResponse(
        "profile.html",
        get_template_context(request, user_data=user_data, bookings=bookings)
    )


# ==================== SEAT SELECTION & BOOKING ROUTES ====================

@app.get("/bus/{schedule_id}", response_class=HTMLResponse)
async def seat_selection(request: Request, schedule_id: int):
    """Seat selection page."""
    user = get_current_user_from_cookie(request)
    
    db = await get_db()
    
    # Get schedule details
    cursor = await db.execute("""
        SELECT s.*, r.origin, r.destination, r.base_price,
               b.name as bus_name, b.bus_type, b.total_seats, b.seat_layout, b.amenities
        FROM schedules s
        JOIN routes r ON s.route_id = r.id
        JOIN buses b ON s.bus_id = b.id
        WHERE s.id = ?
    """, (schedule_id,))
    
    schedule = await cursor.fetchone()
    if not schedule:
        await db.close()
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    schedule = dict(schedule)
    schedule["seat_layout"] = json.loads(schedule["seat_layout"]) if schedule["seat_layout"] else {"rows": 10, "cols": 4}
    schedule["amenities"] = json.loads(schedule["amenities"]) if schedule["amenities"] else []
    schedule["final_price"] = schedule["base_price"] * schedule["price_multiplier"]
    
    # Get seat status
    seat_status = await get_seat_status(schedule_id)
    
    # Check hold availability
    can_hold, hold_type, hold_message = await can_create_hold(schedule_id)
    
    # Get user's current holds
    user_holds = []
    if user:
        cursor = await db.execute("""
            SELECT sh.*, s.seat_number
            FROM seat_holds sh
            JOIN seats s ON sh.seat_id = s.id
            WHERE sh.user_id = ? AND sh.schedule_id = ? AND sh.status = 'active'
        """, (user["sub"], schedule_id))
        user_holds = [dict(row) for row in await cursor.fetchall()]
    
    await db.close()
    
    return templates.TemplateResponse(
        "seat_selection.html",
        get_template_context(
            request,
            schedule=schedule,
            seats=seat_status,
            can_hold=can_hold,
            hold_type=hold_type,
            hold_message=hold_message,
            user_holds=user_holds
        )
    )


@app.post("/api/hold")
async def create_hold(request: Request):
    """Create seat hold via AJAX."""
    user = get_current_user_from_cookie(request)
    if not user:
        return JSONResponse({"error": "Please login to hold seats"}, status_code=401)
    
    data = await request.json()
    seat_ids = data.get("seat_ids", [])
    schedule_id = data.get("schedule_id")
    
    if not seat_ids or not schedule_id:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    
    try:
        # Determine hold type
        can_hold, hold_type, message = await can_create_hold(schedule_id)
        if not can_hold:
            return JSONResponse({"error": message}, status_code=400)
        
        hold_ids = await create_seat_hold(
            user_id=int(user["sub"]),
            seat_ids=seat_ids,
            schedule_id=schedule_id,
            hold_type=hold_type
        )
        
        return JSONResponse({
            "success": True,
            "hold_ids": hold_ids,
            "hold_type": hold_type,
            "message": f"Seats held successfully ({message})"
        })
    
    except HoldNotAllowedError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except SeatUnavailableError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": "Failed to hold seats"}, status_code=500)


@app.post("/api/release-holds")
async def release_holds(request: Request):
    """Release user's holds via AJAX."""
    user = get_current_user_from_cookie(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    
    data = await request.json()
    schedule_id = data.get("schedule_id")
    
    if not schedule_id:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    
    count = await release_user_holds(int(user["sub"]), schedule_id)
    
    return JSONResponse({
        "success": True,
        "released_count": count
    })


@app.get("/api/seats/{schedule_id}")
async def get_seats(schedule_id: int):
    """Get current seat status via AJAX."""
    seat_status = await get_seat_status(schedule_id)
    return JSONResponse({"seats": seat_status})


@app.get("/checkout/{schedule_id}", response_class=HTMLResponse)
async def checkout_page(request: Request, schedule_id: int):
    """Checkout page."""
    user = get_current_user_from_cookie(request)
    if not user:
        return RedirectResponse(f"/login?next=/bus/{schedule_id}", status_code=302)
    
    db = await get_db()
    
    # Get user's active holds for this schedule
    cursor = await db.execute("""
        SELECT sh.*, s.seat_number, s.price_premium
        FROM seat_holds sh
        JOIN seats s ON sh.seat_id = s.id
        WHERE sh.user_id = ? AND sh.schedule_id = ? AND sh.status = 'active'
    """, (user["sub"], schedule_id))
    holds = [dict(row) for row in await cursor.fetchall()]
    
    if not holds:
        await db.close()
        return RedirectResponse(f"/bus/{schedule_id}?error=no_holds", status_code=302)
    
    # Get schedule details
    cursor = await db.execute("""
        SELECT s.*, r.origin, r.destination, r.base_price,
               b.name as bus_name, b.bus_type
        FROM schedules s
        JOIN routes r ON s.route_id = r.id
        JOIN buses b ON s.bus_id = b.id
        WHERE s.id = ?
    """, (schedule_id,))
    
    schedule = dict(await cursor.fetchone())
    schedule["final_price"] = schedule["base_price"] * schedule["price_multiplier"]
    
    await db.close()
    
    # Calculate total
    total = sum(schedule["final_price"] + h["price_premium"] for h in holds)
    
    return templates.TemplateResponse(
        "checkout.html",
        get_template_context(
            request,
            schedule=schedule,
            holds=holds,
            total=total
        )
    )


@app.post("/checkout/{schedule_id}")
async def process_checkout(request: Request, schedule_id: int):
    """Process checkout and create booking."""
    user = get_current_user_from_cookie(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    # Get user's active holds
    cursor = await db.execute("""
        SELECT sh.seat_id, s.price_premium
        FROM seat_holds sh
        JOIN seats s ON sh.seat_id = s.id
        WHERE sh.user_id = ? AND sh.schedule_id = ? AND sh.status = 'active'
    """, (user["sub"], schedule_id))
    holds = await cursor.fetchall()
    
    if not holds:
        await db.close()
        return RedirectResponse(f"/bus/{schedule_id}?error=holds_expired", status_code=302)
    
    seat_ids = [h["seat_id"] for h in holds]
    
    # Get base price
    cursor = await db.execute("""
        SELECT r.base_price * s.price_multiplier as final_price
        FROM schedules s
        JOIN routes r ON s.route_id = r.id
        WHERE s.id = ?
    """, (schedule_id,))
    price_row = await cursor.fetchone()
    base_price = price_row["final_price"]
    
    # Calculate total
    total = sum(base_price + h["price_premium"] for h in holds)
    
    await db.close()
    
    # Create booking (simulated payment success)
    booking_ref = await create_booking(
        user_id=int(user["sub"]),
        schedule_id=schedule_id,
        seat_ids=seat_ids,
        total_price=total
    )
    
    return RedirectResponse(f"/booking/{booking_ref}", status_code=302)


@app.get("/booking/{booking_ref}", response_class=HTMLResponse)
async def booking_confirmation(request: Request, booking_ref: str):
    """Booking confirmation page."""
    booking = await get_booking_by_reference(booking_ref)
    
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    # Get seat numbers
    db = await get_db()
    placeholders = ",".join("?" * len(booking["seat_ids"]))
    cursor = await db.execute(
        f"SELECT seat_number FROM seats WHERE id IN ({placeholders})",
        booking["seat_ids"]
    )
    seat_numbers = [row["seat_number"] for row in await cursor.fetchall()]
    await db.close()
    
    # Generate booking QR code
    qr_code = generate_qr_code(f"busreserve://booking/{booking_ref}")
    
    return templates.TemplateResponse(
        "booking_confirmation.html",
        get_template_context(
            request,
            booking=booking,
            seat_numbers=seat_numbers,
            qr_code=qr_code
        )
    )


# ==================== TRACKING ROUTES ====================

@app.get("/tracking/{schedule_id}", response_class=HTMLResponse)
async def tracking_page(request: Request, schedule_id: int):
    """Real-time bus tracking page."""
    db = await get_db()
    
    cursor = await db.execute("""
        SELECT s.*, r.origin, r.destination,
               b.name as bus_name, b.registration_number
        FROM schedules s
        JOIN routes r ON s.route_id = r.id
        JOIN buses b ON s.bus_id = b.id
        WHERE s.id = ?
    """, (schedule_id,))
    
    schedule = await cursor.fetchone()
    await db.close()
    
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    return templates.TemplateResponse(
        "tracking.html",
        get_template_context(request, schedule=dict(schedule))
    )


@app.get("/api/tracking/{schedule_id}")
async def get_bus_location(schedule_id: int):
    """Get current bus location."""
    db = await get_db()
    
    cursor = await db.execute("""
        SELECT current_lat, current_lng, departure_time, arrival_time
        FROM schedules WHERE id = ?
    """, (schedule_id,))
    
    schedule = await cursor.fetchone()
    await db.close()
    
    if not schedule:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)
    
    # Simulate location based on time progress
    import random
    
    departure = datetime.fromisoformat(schedule["departure_time"])
    arrival = datetime.fromisoformat(schedule["arrival_time"])
    now = datetime.now()
    
    if now < departure:
        # Not started yet - return origin approximate
        lat = 40.7128 + random.uniform(-0.01, 0.01)
        lng = -74.0060 + random.uniform(-0.01, 0.01)
        status = "not_started"
    elif now > arrival:
        # Arrived
        lat = 42.3601 + random.uniform(-0.01, 0.01)
        lng = -71.0589 + random.uniform(-0.01, 0.01)
        status = "arrived"
    else:
        # In progress - interpolate
        total_duration = (arrival - departure).total_seconds()
        elapsed = (now - departure).total_seconds()
        progress = elapsed / total_duration
        
        # Simple linear interpolation between NYC and Boston
        start_lat, start_lng = 40.7128, -74.0060
        end_lat, end_lng = 42.3601, -71.0589
        
        lat = start_lat + (end_lat - start_lat) * progress + random.uniform(-0.02, 0.02)
        lng = start_lng + (end_lng - start_lng) * progress + random.uniform(-0.02, 0.02)
        status = "in_transit"
    
    return JSONResponse({
        "lat": lat,
        "lng": lng,
        "status": status,
        "updated_at": datetime.now().isoformat()
    })


# ==================== ADMIN ROUTES ====================

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Admin dashboard."""
    user = get_current_user_from_cookie(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    # Get stats
    cursor = await db.execute("SELECT COUNT(*) as count FROM users WHERE role = 'customer'")
    customer_count = (await cursor.fetchone())["count"]
    
    cursor = await db.execute("SELECT COUNT(*) as count FROM buses")
    bus_count = (await cursor.fetchone())["count"]
    
    cursor = await db.execute("SELECT COUNT(*) as count FROM bookings WHERE payment_status = 'completed'")
    booking_count = (await cursor.fetchone())["count"]
    
    cursor = await db.execute("SELECT SUM(total_price) as total FROM bookings WHERE payment_status = 'completed'")
    revenue = (await cursor.fetchone())["total"] or 0
    
    cursor = await db.execute("SELECT COUNT(*) as count FROM seat_holds WHERE status = 'active'")
    active_holds = (await cursor.fetchone())["count"]
    
    # Recent bookings
    cursor = await db.execute("""
        SELECT b.*, u.name as customer_name, u.email as customer_email,
               r.origin, r.destination
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        JOIN schedules s ON b.schedule_id = s.id
        JOIN routes r ON s.route_id = r.id
        ORDER BY b.created_at DESC LIMIT 10
    """)
    recent_bookings = [dict(row) for row in await cursor.fetchall()]
    
    await db.close()
    
    return templates.TemplateResponse(
        "admin/dashboard.html",
        get_template_context(
            request,
            stats={
                "customers": customer_count,
                "buses": bus_count,
                "bookings": booking_count,
                "revenue": revenue,
                "active_holds": active_holds
            },
            recent_bookings=recent_bookings
        )
    )


@app.get("/admin/buses", response_class=HTMLResponse)
async def admin_buses(request: Request):
    """Admin bus management."""
    user = get_current_user_from_cookie(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    cursor = await db.execute("""
        SELECT b.*, u.name as created_by_name
        FROM buses b
        LEFT JOIN users u ON b.created_by = u.id
        ORDER BY b.created_at DESC
    """)
    buses = []
    for row in await cursor.fetchall():
        bus = dict(row)
        bus["amenities"] = json.loads(bus["amenities"]) if bus["amenities"] else []
        buses.append(bus)
    
    await db.close()
    
    return templates.TemplateResponse(
        "admin/buses.html",
        get_template_context(request, buses=buses)
    )


@app.get("/admin/buses/new", response_class=HTMLResponse)
async def admin_new_bus(request: Request):
    """Add new bus form."""
    user = get_current_user_from_cookie(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/login", status_code=302)
    
    return templates.TemplateResponse(
        "admin/bus_form.html",
        get_template_context(request, bus=None)
    )


@app.post("/admin/buses/new")
async def admin_create_bus(
    request: Request,
    name: str = Form(...),
    registration_number: str = Form(...),
    bus_type: str = Form(...),
    rows: int = Form(...),
    cols: int = Form(...),
    amenities: str = Form("")
):
    """Create new bus."""
    user = get_current_user_from_cookie(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    total_seats = rows * cols
    seat_layout = json.dumps({"rows": rows, "cols": cols})
    amenities_list = json.dumps([a.strip() for a in amenities.split(",") if a.strip()])
    
    try:
        cursor = await db.execute("""
            INSERT INTO buses (name, registration_number, bus_type, total_seats, seat_layout, amenities, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, registration_number, bus_type, total_seats, seat_layout, amenities_list, user["sub"]))
        
        bus_id = cursor.lastrowid
        
        # Create seats
        for row in range(rows):
            row_letter = chr(65 + row)
            for col in range(1, cols + 1):
                seat_number = f"{row_letter}{col}"
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
        
        await db.commit()
        await db.close()
        
        return RedirectResponse("/admin/buses", status_code=302)
    
    except Exception as e:
        await db.rollback()
        await db.close()
        return templates.TemplateResponse(
            "admin/bus_form.html",
            get_template_context(request, bus=None, error=str(e))
        )


@app.get("/admin/routes", response_class=HTMLResponse)
async def admin_routes(request: Request):
    """Admin route management."""
    user = get_current_user_from_cookie(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    cursor = await db.execute("SELECT * FROM routes ORDER BY origin, destination")
    routes = [dict(row) for row in await cursor.fetchall()]
    
    await db.close()
    
    return templates.TemplateResponse(
        "admin/routes.html",
        get_template_context(request, routes=routes)
    )


@app.post("/admin/routes/new")
async def admin_create_route(
    request: Request,
    origin: str = Form(...),
    destination: str = Form(...),
    distance_km: float = Form(...),
    base_price: float = Form(...)
):
    """Create new route."""
    user = get_current_user_from_cookie(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    await db.execute("""
        INSERT INTO routes (origin, destination, distance_km, base_price)
        VALUES (?, ?, ?, ?)
    """, (origin, destination, distance_km, base_price))
    
    await db.commit()
    await db.close()
    
    return RedirectResponse("/admin/routes", status_code=302)


@app.get("/admin/schedules", response_class=HTMLResponse)
async def admin_schedules(request: Request):
    """Admin schedule management."""
    user = get_current_user_from_cookie(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    cursor = await db.execute("""
        SELECT s.*, b.name as bus_name, r.origin, r.destination, r.base_price
        FROM schedules s
        JOIN buses b ON s.bus_id = b.id
        JOIN routes r ON s.route_id = r.id
        ORDER BY s.departure_time DESC
    """)
    schedules = [dict(row) for row in await cursor.fetchall()]
    
    cursor = await db.execute("SELECT id, name FROM buses ORDER BY name")
    buses = [dict(row) for row in await cursor.fetchall()]
    
    cursor = await db.execute("SELECT id, origin, destination FROM routes ORDER BY origin")
    routes = [dict(row) for row in await cursor.fetchall()]
    
    await db.close()
    
    return templates.TemplateResponse(
        "admin/schedules.html",
        get_template_context(request, schedules=schedules, buses=buses, routes=routes)
    )


@app.post("/admin/schedules/new")
async def admin_create_schedule(
    request: Request,
    bus_id: int = Form(...),
    route_id: int = Form(...),
    departure_time: str = Form(...),
    arrival_time: str = Form(...),
    price_multiplier: float = Form(1.0)
):
    """Create new schedule."""
    user = get_current_user_from_cookie(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/login", status_code=302)
    
    db = await get_db()
    
    await db.execute("""
        INSERT INTO schedules (bus_id, route_id, departure_time, arrival_time, price_multiplier)
        VALUES (?, ?, ?, ?, ?)
    """, (bus_id, route_id, departure_time, arrival_time, price_multiplier))
    
    await db.commit()
    await db.close()
    
    return RedirectResponse("/admin/schedules", status_code=302)
if __name__ == "__main__":
    import uvicorn
    # Use the port Render/Vercel provides, or default to 8000
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
import uuid
import json

from database import get_db


class HoldNotAllowedError(Exception):
    """Raised when a hold cannot be created."""
    pass


class SeatUnavailableError(Exception):
    """Raised when a seat is not available."""
    pass


async def get_seat_status(schedule_id: int) -> dict:
    """
    Get status of all seats for a schedule.
    Returns dict mapping seat_id to status: 'available', 'held', 'booked'
    """
    db = await get_db()
    
    # Get all seats for the bus in this schedule
    cursor = await db.execute("""
        SELECT s.id, s.seat_number, s.seat_type, s.price_premium,
               b.id as bus_id, b.seat_layout
        FROM seats s
        JOIN schedules sch ON s.bus_id = sch.bus_id
        JOIN buses b ON s.bus_id = b.id
        WHERE sch.id = ?
        ORDER BY s.seat_number
    """, (schedule_id,))
    seats = await cursor.fetchall()
    
    # Get active holds
    cursor = await db.execute("""
        SELECT seat_id, user_id, hold_type, expires_at
        FROM seat_holds
        WHERE schedule_id = ? AND status = 'active'
    """, (schedule_id,))
    holds = {row["seat_id"]: dict(row) for row in await cursor.fetchall()}
    
    # Get completed bookings
    cursor = await db.execute("""
        SELECT seat_ids FROM bookings
        WHERE schedule_id = ? AND payment_status = 'completed'
    """, (schedule_id,))
    booked_seats = set()
    for row in await cursor.fetchall():
        booked_seats.update(json.loads(row["seat_ids"]))
    
    await db.close()
    
    result = {}
    for seat in seats:
        seat_id = seat["id"]
        if seat_id in booked_seats:
            status = "booked"
        elif seat_id in holds:
            status = "held"
        else:
            status = "available"
        
        result[seat_id] = {
            "id": seat_id,
            "seat_number": seat["seat_number"],
            "seat_type": seat["seat_type"],
            "price_premium": seat["price_premium"],
            "status": status,
            "hold_info": holds.get(seat_id)
        }
    
    return result


async def can_create_hold(schedule_id: int) -> Tuple[bool, str, str]:
    """
    Check if holds can be created for this schedule.
    Returns: (can_hold, hold_type, message)
    """
    db = await get_db()
    
    cursor = await db.execute("""
        SELECT departure_time FROM schedules WHERE id = ?
    """, (schedule_id,))
    schedule = await cursor.fetchone()
    await db.close()
    
    if not schedule:
        return False, "", "Schedule not found"
    
    departure = datetime.fromisoformat(schedule["departure_time"])
    now = datetime.now()
    hours_until_departure = (departure - now).total_seconds() / 3600
    
    if hours_until_departure <= 0:
        return False, "", "This bus has already departed"
    
    if hours_until_departure <= 24:
        # Within 24 hours - no early booking holds, only checkout holds
        return True, "checkout", "Checkout hold (30 minutes)"
    
    # More than 24 hours - allow early booking holds
    return True, "early_booking", "Early booking hold (12 hours)"


async def create_seat_hold(
    user_id: int,
    seat_ids: List[int],
    schedule_id: int,
    hold_type: str = "checkout"
) -> List[int]:
    """
    Create holds for seats.
    Returns list of hold IDs.
    """
    db = await get_db()
    
    # Check if schedule allows holds
    can_hold, allowed_type, message = await can_create_hold(schedule_id)
    if not can_hold:
        await db.close()
        raise HoldNotAllowedError(message)
    
    # Determine hold duration based on type
    now = datetime.now()
    if hold_type == "early_booking":
        expires_at = now + timedelta(hours=12)
    else:  # checkout
        expires_at = now + timedelta(minutes=30)
    
    hold_ids = []
    
    try:
        for seat_id in seat_ids:
            # Check if seat is available
            cursor = await db.execute("""
                SELECT id FROM seat_holds
                WHERE seat_id = ? AND schedule_id = ? AND status = 'active'
            """, (seat_id, schedule_id))
            existing = await cursor.fetchone()
            
            if existing:
                raise SeatUnavailableError(f"Seat {seat_id} is already on hold")
            
            # Check if seat is booked
            cursor = await db.execute("""
                SELECT id, seat_ids FROM bookings
                WHERE schedule_id = ? AND payment_status = 'completed'
            """, (schedule_id,))
            bookings = await cursor.fetchall()
            
            for booking in bookings:
                if seat_id in json.loads(booking["seat_ids"]):
                    raise SeatUnavailableError(f"Seat {seat_id} is already booked")
            
            # Create hold
            cursor = await db.execute("""
                INSERT INTO seat_holds (seat_id, schedule_id, user_id, hold_type, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (seat_id, schedule_id, user_id, hold_type, expires_at.isoformat()))
            hold_ids.append(cursor.lastrowid)
        
        await db.commit()
    except Exception as e:
        await db.rollback()
        await db.close()
        raise e
    
    await db.close()
    return hold_ids


async def release_seat_hold(hold_id: int, user_id: int) -> bool:
    """Release a specific hold."""
    db = await get_db()
    
    await db.execute("""
        UPDATE seat_holds
        SET status = 'released'
        WHERE id = ? AND user_id = ? AND status = 'active'
    """, (hold_id, user_id))
    
    await db.commit()
    await db.close()
    return True


async def release_user_holds(user_id: int, schedule_id: int) -> int:
    """Release all holds for a user on a schedule."""
    db = await get_db()
    
    cursor = await db.execute("""
        UPDATE seat_holds
        SET status = 'released'
        WHERE user_id = ? AND schedule_id = ? AND status = 'active'
    """, (user_id, schedule_id))
    
    count = cursor.rowcount
    await db.commit()
    await db.close()
    return count


async def release_expired_holds() -> int:
    """
    Release all expired holds.
    Called by the background scheduler.
    """
    db = await get_db()
    now = datetime.now().isoformat()
    
    cursor = await db.execute("""
        UPDATE seat_holds
        SET status = 'released'
        WHERE expires_at < ? AND status = 'active'
    """, (now,))
    
    count = cursor.rowcount
    await db.commit()
    await db.close()
    
    if count > 0:
        print(f"[Scheduler] Released {count} expired holds at {now}")
    
    return count


async def create_booking(
    user_id: int,
    schedule_id: int,
    seat_ids: List[int],
    total_price: float
) -> str:
    """
    Create a booking from held seats.
    Returns booking reference.
    """
    db = await get_db()
    
    # Generate unique booking reference
    booking_ref = f"BUS-{uuid.uuid4().hex[:8].upper()}"
    
    # Convert holds to booking
    await db.execute("""
        UPDATE seat_holds
        SET status = 'converted'
        WHERE user_id = ? AND schedule_id = ? AND seat_id IN ({}) AND status = 'active'
    """.format(",".join("?" * len(seat_ids))), (user_id, schedule_id, *seat_ids))
    
    # Create booking record
    await db.execute("""
        INSERT INTO bookings (user_id, schedule_id, seat_ids, total_price, payment_status, booking_reference)
        VALUES (?, ?, ?, ?, 'completed', ?)
    """, (user_id, schedule_id, json.dumps(seat_ids), total_price, booking_ref))
    
    await db.commit()
    await db.close()
    
    return booking_ref


async def get_user_bookings(user_id: int) -> List[dict]:
    """Get all bookings for a user."""
    db = await get_db()
    
    cursor = await db.execute("""
        SELECT b.*, 
               s.departure_time, s.arrival_time,
               r.origin, r.destination, r.base_price,
               bus.name as bus_name, bus.bus_type
        FROM bookings b
        JOIN schedules s ON b.schedule_id = s.id
        JOIN routes r ON s.route_id = r.id
        JOIN buses bus ON s.bus_id = bus.id
        WHERE b.user_id = ?
        ORDER BY b.created_at DESC
    """, (user_id,))
    
    bookings = [dict(row) for row in await cursor.fetchall()]
    await db.close()
    
    return bookings


async def get_booking_by_reference(booking_ref: str) -> Optional[dict]:
    """Get booking details by reference."""
    db = await get_db()
    
    cursor = await db.execute("""
        SELECT b.*, 
               s.departure_time, s.arrival_time,
               r.origin, r.destination,
               bus.name as bus_name, bus.bus_type, bus.registration_number
        FROM bookings b
        JOIN schedules s ON b.schedule_id = s.id
        JOIN routes r ON s.route_id = r.id
        JOIN buses bus ON s.bus_id = bus.id
        WHERE b.booking_reference = ?
    """, (booking_ref,))
    
    booking = await cursor.fetchone()
    await db.close()
    
    if booking:
        result = dict(booking)
        result["seat_ids"] = json.loads(result["seat_ids"])
        return result
    return None

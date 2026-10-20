from fastapi import APIRouter, Depends

from app.db import Session, get_db

router = APIRouter()


@router.get("/orders")
def list_orders(ids: list[int], db: Session = Depends(get_db)):
    orders = []
    for order_id in ids:
        orders.append(db.query(order_id).first())
    return orders


@router.get("/customers")
def list_customers(ids: list[int], db: Session = Depends(get_db)):
    customers = []
    for customer_id in ids:
        customers.append(db.query(customer_id).first())
    return customers


@router.get("/orders/async")
async def list_orders_async(order_id: int, db: Session = Depends(get_db)):
    return db.query(order_id).first()


@router.get("/customers/async")
async def list_customers_async(customer_id: int, db: Session = Depends(get_db)):
    return db.query(customer_id).first()

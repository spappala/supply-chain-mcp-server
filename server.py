"""
Supply Chain MCP Server
Exposes 3 tools: check_warehouse_stock, run_demand_forecast, place_vendor_order
Reads DB connection from environment variables.
"""

import os
import json
from datetime import date, timedelta

import mysql.connector
from mcp.server.mcpserver import MCPServer

# ── DB config (set these as environment variables) ─────────────────────────────
DB_CONFIG = {
    "host":     os.environ["DB_HOST"],
    "port":     int(os.environ["DB_PORT"]),
    "user":     os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "database": os.environ["DB_NAME"],
}

def get_conn():
    return mysql.connector.connect(**DB_CONFIG)

# ── MCP Server ─────────────────────────────────────────────────────────────────
mcp = MCPServer(
    name="SupplyChainMCPServer",
    instructions=(
        "You are a supply chain assistant. Use the available tools to check "
        "warehouse stock levels, run demand forecasts, and place vendor purchase orders."
    ),
)

# ── Tool 1: Check Warehouse Stock ──────────────────────────────────────────────
@mcp.tool()
def check_warehouse_stock(sku: str) -> str:
    """
    Check the current warehouse stock level for a given SKU across all warehouses.
    Returns total available quantity, stock status (OK / LOW / CRITICAL / OUT OF STOCK),
    and a breakdown per warehouse.

    Args:
        sku: The product SKU code to check (e.g. HFLX-2026)
    """
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT sku, name, reorder_point FROM products WHERE sku = %s", (sku,))
    product = cursor.fetchone()
    if not product:
        cursor.close(); conn.close()
        return json.dumps({"error": f"SKU '{sku}' not found in inventory."})

    cursor.execute("""
        SELECT w.name AS warehouse, w.location,
               ws.quantity_on_hand, ws.quantity_reserved, ws.quantity_available
        FROM warehouse_stock ws
        JOIN warehouses w ON ws.warehouse_id = w.id
        WHERE ws.sku = %s
        ORDER BY w.warehouse_code
    """, (sku,))
    stock_rows = cursor.fetchall()

    total_available = sum(r["quantity_available"] for r in stock_rows)
    reorder_point   = product["reorder_point"]

    if total_available <= 0:
        status = "OUT OF STOCK"
    elif total_available < reorder_point * 0.5:
        status = "CRITICAL"
    elif total_available < reorder_point:
        status = "LOW"
    else:
        status = "OK"

    cursor.close(); conn.close()

    return json.dumps({
        "sku":             sku,
        "product_name":    product["name"],
        "reorder_point":   reorder_point,
        "total_available": total_available,
        "stock_status":    status,
        "warehouses":      stock_rows,
        "recommendation": (
            f"Stock is {status}. Immediate reorder recommended — only {total_available} "
            f"units available against a reorder point of {reorder_point}."
            if status in ("CRITICAL", "LOW", "OUT OF STOCK")
            else f"Stock is healthy with {total_available} units available."
        )
    }, default=str)


# ── Tool 2: Run Demand Forecast ────────────────────────────────────────────────
@mcp.tool()
def run_demand_forecast(sku: str) -> str:
    """
    Run a demand forecast for a given SKU showing predicted demand for the next
    3 months, current stock levels, and the gap that needs to be covered by a
    purchase order.

    Args:
        sku: The product SKU code to forecast (e.g. HFLX-2026)
    """
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT sku, name FROM products WHERE sku = %s", (sku,))
    product = cursor.fetchone()
    if not product:
        cursor.close(); conn.close()
        return json.dumps({"error": f"SKU '{sku}' not found."})

    cursor.execute("""
        SELECT forecast_month, forecasted_qty, actual_qty, confidence_pct
        FROM demand_forecast
        WHERE sku = %s AND forecast_month >= CURDATE()
        ORDER BY forecast_month
        LIMIT 3
    """, (sku,))
    forecasts = cursor.fetchall()

    cursor.execute("""
        SELECT COALESCE(SUM(quantity_available), 0) AS total_available
        FROM warehouse_stock WHERE sku = %s
    """, (sku,))
    stock = cursor.fetchone()
    total_available = int(stock["total_available"]) if stock else 0

    cursor.execute("""
        SELECT ROUND(AVG(monthly_total), 0) AS avg_monthly_sales
        FROM (
            SELECT sale_date, SUM(quantity_sold) AS monthly_total
            FROM sales_history
            WHERE sku = %s AND sale_date >= DATE_SUB(CURDATE(), INTERVAL 3 MONTH)
            GROUP BY sale_date
        ) t
    """, (sku,))
    trend = cursor.fetchone()
    avg_monthly = int(trend["avg_monthly_sales"]) if trend and trend["avg_monthly_sales"] else 0

    cursor.close(); conn.close()

    forecast_with_gap = []
    for f in forecasts:
        gap = max(0, f["forecasted_qty"] - total_available)
        forecast_with_gap.append({
            "month":          str(f["forecast_month"]),
            "forecasted_qty": f["forecasted_qty"],
            "confidence_pct": float(f["confidence_pct"]),
            "stock_gap":      gap,
            "recommendation": "ORDER NEEDED" if gap > 0 else "SUFFICIENT"
        })

    next_month_forecast  = forecasts[0]["forecasted_qty"] if forecasts else 0
    suggested_order_qty  = max(0, next_month_forecast - total_available)

    return json.dumps({
        "sku":                 sku,
        "product_name":        product["name"],
        "current_stock":       total_available,
        "avg_monthly_sales":   avg_monthly,
        "forecast":            forecast_with_gap,
        "suggested_order_qty": suggested_order_qty,
        "summary": (
            f"Current stock of {total_available} units is insufficient for forecasted demand. "
            f"Recommended order quantity: {suggested_order_qty} units to cover next month's demand."
            if suggested_order_qty > 0
            else f"Current stock of {total_available} units is sufficient for forecasted demand."
        )
    }, default=str)


# ── Tool 3: Place Vendor Order ─────────────────────────────────────────────────
@mcp.tool()
def place_vendor_order(sku: str, quantity: int, notes: str = "") -> str:
    """
    Place a purchase order for a given SKU and quantity from the primary vendor.
    Creates a new PO record in the database with status SUBMITTED.

    Args:
        sku:      The product SKU to order (e.g. HFLX-2026)
        quantity: Number of units to order (e.g. 500)
        notes:    Optional notes for the purchase order
    """
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT p.sku, p.name, p.unit_cost, p.vendor_id,
               v.name AS vendor_name, v.lead_time_days, v.contact_email
        FROM products p
        JOIN vendors v ON p.vendor_id = v.id
        WHERE p.sku = %s
    """, (sku,))
    product = cursor.fetchone()
    if not product:
        cursor.close(); conn.close()
        return json.dumps({"error": f"SKU '{sku}' not found."})

    cursor.execute("SELECT COUNT(*) AS cnt FROM purchase_orders")
    count         = cursor.fetchone()["cnt"]
    po_number     = f"PO-2026-{1000 + count + 1}"
    unit_cost     = float(product["unit_cost"])
    total_cost    = unit_cost * quantity
    expected_date = (date.today() + timedelta(days=product["lead_time_days"])).isoformat()
    po_notes      = notes if notes else f"AI-generated order based on demand forecast for {sku}"

    cursor.execute("""
        INSERT INTO purchase_orders
            (po_number, vendor_id, sku, quantity, unit_cost, total_cost,
             status, expected_date, notes, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, 'SUBMITTED', %s, %s, 'supply-chain-agent')
    """, (po_number, product["vendor_id"], sku, quantity,
          unit_cost, total_cost, expected_date, po_notes))
    conn.commit()
    cursor.close(); conn.close()

    return json.dumps({
        "success":       True,
        "po_number":     po_number,
        "sku":           sku,
        "product_name":  product["name"],
        "vendor":        product["vendor_name"],
        "vendor_email":  product["contact_email"],
        "quantity":      quantity,
        "unit_cost":     unit_cost,
        "total_cost":    total_cost,
        "status":        "SUBMITTED",
        "expected_date": expected_date,
        "message": (
            f"Purchase order {po_number} successfully submitted to {product['vendor_name']} "
            f"for {quantity} units of {sku}. Expected delivery: {expected_date}."
        )
    }, default=str)


# ── Tool 4: Get Open Purchase Orders ───────────────────────────────────────────
@mcp.tool()
def get_open_purchase_orders(sku: str) -> str:
    """
    List OPEN (in-flight) purchase orders for a SKU — orders with status DRAFT,
    SUBMITTED, CONFIRMED, or SHIPPED (i.e. not yet RECEIVED or CANCELLED). Use this
    to check for outstanding commitments against a vendor contract before deciding
    whether the contract can be safely cancelled.

    Args:
        sku: The product SKU code to check (e.g. HFLX-2026)
    """
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT sku, name FROM products WHERE sku = %s", (sku,))
    product = cursor.fetchone()
    if not product:
        cursor.close(); conn.close()
        return json.dumps({"error": f"SKU '{sku}' not found."})

    cursor.execute("""
        SELECT po.po_number, po.quantity, po.unit_cost, po.total_cost, po.status,
               po.order_date, po.expected_date, v.name AS vendor_name
        FROM purchase_orders po
        JOIN vendors v ON po.vendor_id = v.id
        WHERE po.sku = %s
          AND po.status IN ('DRAFT', 'SUBMITTED', 'CONFIRMED', 'SHIPPED')
        ORDER BY po.order_date
    """, (sku,))
    open_pos = cursor.fetchall()

    cursor.close(); conn.close()

    open_quantity = sum(int(p["quantity"]) for p in open_pos)
    open_value    = sum(float(p["total_cost"]) for p in open_pos)

    return json.dumps({
        "sku":                  sku,
        "product_name":         product["name"],
        "open_po_count":        len(open_pos),
        "open_quantity":        open_quantity,
        "open_value":           open_value,
        "open_purchase_orders": open_pos,
        "summary": (
            f"{len(open_pos)} open purchase order(s) totalling {open_quantity} units "
            f"(${open_value:,.2f}) are still in flight against this SKU. These are "
            f"outstanding commitments to consider before cancelling the contract."
            if open_pos
            else f"No open purchase orders for {sku}. There are no outstanding "
                 f"in-flight commitments blocking a contract cancellation."
        )
    }, default=str)


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Stateless + plain-JSON responses so strict MCP clients (e.g. the MuleSoft
    # Agent Broker) don't depend on session-id correlation over SSE, which was
    # causing discarded responses and 60s broker timeouts.
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        stateless_http=True,
        json_response=True,
    )

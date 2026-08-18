"""
Seed script: load fms_accounting demo data into an existing DB that has
fms + fms_accounting installed but was created without demo mode.

Run via:
    make odoo-shell < fms_accounting/scripts/seed_accounting_demo.py
or:
    /path/to/odoo-bin shell -d fms_e2e ... < seed_accounting_demo.py
"""

Product = env['product.product']
Location = env['stock.location']
Employee = env['hr.employee']
Partner = env['res.partner']
Shift = env['fms.shift']
Delivery = env['fms.fuel.delivery']
DeliveryLine = env['fms.fuel.delivery.line']
CashDrop = env['fms.cash.drop']
Move = env['account.move']

# ── helpers ───────────────────────────────────────────────────────────────────

def find(model, domain):
    return env[model].search(domain, limit=1)

def exists(model, domain):
    return bool(env[model].search(domain, limit=1))

print("=== FMS Accounting Demo Seed ===")

# ── 1. Credit customers ───────────────────────────────────────────────────────

customers = [
    dict(
        name='NSS Security Ltd',
        company_type='company',
        street='Upper Hill, Nairobi',
        phone='+254 700 100 200',
        email='accounts@nss.co.ke',
        customer_rank=1,
        fms_is_fleet_customer=True,
        fms_credit_limit=500000.0,
    ),
    dict(
        name='Nairobi County Government',
        company_type='company',
        street='City Hall Way, Nairobi',
        phone='+254 20 2228000',
        email='fleet@nairobicity.go.ke',
        customer_rank=1,
        fms_is_fleet_customer=True,
        fms_credit_limit=1000000.0,
    ),
    dict(
        name='Eastleigh Shuttle Sacco',
        company_type='company',
        street='Eastleigh, Nairobi',
        phone='+254 722 300 400',
        email='sacco@eastleighshuttle.co.ke',
        customer_rank=1,
        fms_is_fleet_customer=True,
        fms_credit_limit=200000.0,
    ),
]

partner_map = {}
for c in customers:
    rec = find('res.partner', [('name', '=', c['name'])])
    if not rec:
        rec = Partner.create(c)
        print(f"  Created customer: {c['name']}")
    else:
        print(f"  Exists: {c['name']}")
    partner_map[c['name']] = rec

# ── 2. Suppliers ──────────────────────────────────────────────────────────────

suppliers = [
    dict(name='Vivo Energy Kenya Ltd', supplier_rank=1,
         street='Waiyaki Way, Westlands, Nairobi',
         phone='+254 20 375 2000', email='invoices@vivoenergy.co.ke'),
    dict(name='Kenya Power & Lighting Co.', supplier_rank=1),
    dict(name='Nairobi Water Company', supplier_rank=1),
]

for s in suppliers:
    if not exists('res.partner', [('name', '=', s['name'])]):
        Partner.create(s)
        print(f"  Created supplier: {s['name']}")
    else:
        print(f"  Exists: {s['name']}")

vivo = find('res.partner', [('name', '=', 'Vivo Energy Kenya Ltd')])
kplc = find('res.partner', [('name', '=', 'Kenya Power & Lighting Co.')])
water = find('res.partner', [('name', '=', 'Nairobi Water Company')])

# ── 3. Fuel products + locations (look up from existing data) ─────────────────

diesel = find('product.product', [('name', '=', 'Diesel')])
super_ = find('product.product', [('name', '=', 'Super (Unleaded)')])
vpower = find('product.product', [('name', '=', 'V-Power')])

diesel_tank_1 = find('stock.location', [('name', '=', 'Diesel Tank 1')])
super_tank    = find('stock.location', [('name', '=', 'Super Tank')])
vpower_tank   = find('stock.location', [('name', '=', 'V-Power Tank')])

# ── 4. Fuel deliveries ────────────────────────────────────────────────────────

if vivo and diesel and diesel_tank_1:
    if not exists('fms.fuel.delivery', [('tanker_ref', '=', 'VE-TK-20260110-001')]):
        d1 = Delivery.create({
            'delivery_date': '2026-01-10',
            'tanker_ref': 'VE-TK-20260110-001',
            'vendor_id': vivo.id,
            'notes': 'Morning delivery — Diesel + Super restock',
        })
        DeliveryLine.create({
            'delivery_id': d1.id,
            'product_id': diesel.id,
            'location_id': diesel_tank_1.id,
            'quantity_litres': 10000.0,
            'unit_price': 180.0,
            'dip_before': 8500.0,
            'sales_during': 120.0,
            'dip_after': 18500.0,
        })
        if super_ and super_tank:
            DeliveryLine.create({
                'delivery_id': d1.id,
                'product_id': super_.id,
                'location_id': super_tank.id,
                'quantity_litres': 6000.0,
                'unit_price': 175.0,
                'dip_before': 6100.0,
                'sales_during': 80.0,
                'dip_after': 12000.0,
            })
        print("  Created delivery: VE-TK-20260110-001")
    else:
        print("  Exists: delivery VE-TK-20260110-001")

    if vpower and vpower_tank:
        if not exists('fms.fuel.delivery', [('tanker_ref', '=', 'VE-TK-20260112-001')]):
            d2 = Delivery.create({
                'delivery_date': '2026-01-12',
                'tanker_ref': 'VE-TK-20260112-001',
                'vendor_id': vivo.id,
                'notes': 'V-Power + Kerosene top-up',
            })
            DeliveryLine.create({
                'delivery_id': d2.id,
                'product_id': vpower.id,
                'location_id': vpower_tank.id,
                'quantity_litres': 3000.0,
                'unit_price': 195.0,
                'dip_before': 2100.0,
                'sales_during': 30.0,
                'dip_after': 5000.0,
            })
            print("  Created delivery: VE-TK-20260112-001")
        else:
            print("  Exists: delivery VE-TK-20260112-001")

# ── 5. Customer invoices (draft) ──────────────────────────────────────────────

nss    = partner_map.get('NSS Security Ltd')
county = partner_map.get('Nairobi County Government')
sacco  = partner_map.get('Eastleigh Shuttle Sacco')

invoice_specs = [
    (nss,    '2026-01-08', '2026-02-08', 'Diesel — Fleet Supply Jan 2026',           2500, 222.80),
    (county, '2026-01-09', '2026-02-09', 'Super (Unleaded) — Fleet Supply Jan 2026', 1800, 217.50),
    (sacco,  '2026-01-11', '2026-01-26', 'Diesel — Shuttle Fleet Supply Jan 2026',    800, 222.80),
]

for partner, inv_date, due_date, desc, qty, price in invoice_specs:
    if partner and not exists('account.move', [
        ('partner_id', '=', partner.id),
        ('move_type', '=', 'out_invoice'),
        ('invoice_date', '=', inv_date),
    ]):
        Move.create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': inv_date,
            'invoice_date_due': due_date,
            'invoice_line_ids': [(0, 0, {
                'name': desc,
                'quantity': qty,
                'price_unit': price,
            })],
        })
        print(f"  Created invoice: {partner.name} {inv_date}")
    else:
        print(f"  Exists: invoice {partner.name if partner else '?'} {inv_date}")

# ── 6. Vendor bills (draft) ───────────────────────────────────────────────────

bill_specs = [
    (kplc,  '2026-01-05', '2026-01-20', 'Electricity — January 2026', 1, 48500.0),
    (water, '2026-01-06', '2026-01-21', 'Water — January 2026',        1,  8200.0),
]

for partner, inv_date, due_date, desc, qty, price in bill_specs:
    if partner and not exists('account.move', [
        ('partner_id', '=', partner.id),
        ('move_type', '=', 'in_invoice'),
        ('invoice_date', '=', inv_date),
    ]):
        Move.create({
            'move_type': 'in_invoice',
            'partner_id': partner.id,
            'invoice_date': inv_date,
            'invoice_date_due': due_date,
            'invoice_line_ids': [(0, 0, {
                'name': desc,
                'quantity': qty,
                'price_unit': price,
            })],
        })
        print(f"  Created bill: {partner.name} {inv_date}")
    else:
        print(f"  Exists: bill {partner.name if partner else '?'} {inv_date}")

# ── 7. Cash drops on demo shift ───────────────────────────────────────────────

shift = find('fms.shift', [('label', '=', '1_day')])
alice = find('hr.employee', [('name', '=', 'Alice Wanjiku')])
bob   = find('hr.employee', [('name', '=', 'Bob Otieno')])
carol = find('hr.employee', [('name', '=', 'Carol Muthoni')])

drops = [
    (alice, 80000.0,  '2026-01-15 09:30:00', 'Morning drop'),
    (alice, 70000.0,  '2026-01-15 13:00:00', 'Afternoon drop'),
    (bob,   100000.0, '2026-01-15 10:00:00', 'Full drop — Diesel + Kerosene'),
    (carol, 26874.0,  '2026-01-15 11:00:00', 'V-Power full drop'),
]

if shift:
    for attendant, amount, dropped_at, note in drops:
        if attendant and not exists('fms.cash.drop', [
            ('shift_id', '=', shift.id),
            ('attendant_id', '=', attendant.id),
            ('amount', '=', amount),
        ]):
            CashDrop.create({
                'shift_id': shift.id,
                'attendant_id': attendant.id,
                'amount': amount,
                'dropped_at': dropped_at,
                'note': note,
            })
            print(f"  Created cash drop: {attendant.name} {amount}")
        else:
            print(f"  Exists: cash drop {attendant.name if attendant else '?'} {amount}")
else:
    print("  WARN: demo shift '1_day' not found — skipping cash drops")

env.cr.commit()
print("=== Done ===")

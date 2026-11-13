"""Seed the full basic dataset: real groups/suppliers/zones plus demo data.

Running it on a fresh database creates:

- zones (the real catalog of Havana municipalities)
- suppliers / TCPs (the real ones)
- broadcast_groups (the real channels, including their `peer_id`)
- group_categories, group_zones, supplier_delivery_zones (group configuration)
- demo categories, products and broadcast campaigns

It NEVER invents groups, channels, suppliers (TCPs) or zones: those come from
the real data embedded below. Everything else is demo data on top of them.
The user's own products and campaigns are intentionally not seeded.

The only sensitive values (each supplier's `telegram_id` and each group's
`target` invite link) are NOT embedded here: they are loaded at runtime from
`backend/data/seed_real.json`, which is ignored by git. Copy the schema from
`cuadra_doc/09_seed_demo_data.md` if the file is missing.

Everything is matched by natural key (name, channel+target, template), so the
script is idempotent: running it twice only reports the rows that already exist.

Scheduling:
- `--schedule auto` (default): if the database has no campaigns yet, new
  campaigns are due immediately (`next_send_at = NULL`); otherwise they get a
  staggered future `next_send_at` so a running scheduler does not publish them
  while seeding.
- `--schedule now`: always due immediately (for a fresh DB you are about to test).
- `--schedule future`: always staggered into the future (safe on a live DB).

Usage:
    .venv\\Scripts\\python.exe scripts/seed_demo_data.py
    .venv\\Scripts\\python.exe scripts/seed_demo_data.py --dry-run
    .venv\\Scripts\\python.exe scripts/seed_demo_data.py --schedule now
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuadra import db

# --------------------------------------------------------------------------
# Real data (taken from the production database). Do not invent groups,
# channels, suppliers or zones: keep this section synced with real accounts.
#
# `SUPPLIERS[*].telegram_id` and `GROUPS[*].target` are added at runtime from
# the local file below, so they never reach the git history.
# --------------------------------------------------------------------------

REAL_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_real.json"

ZONES = [
    ("Playa", 1),
    ("La Lisa", 1),
    ("Boyeros", 1),
    ("Centro Habana", 1),
    ("Habana del Este", 1),
    ("San Miguel", 1),
    ("Arroyo Naranjo", 1),
    ("Cerro", 1),
    ("Cotorro", 1),
    ("Diez de Octubre", 1),
    ("Guanabacoa", 1),
    ("Habana Vieja", 1),
    ("Marianao", 1),
    ("Plaza de la Revolución", 1),
    ("Regla", 1),
    ("UCI", 0),
]

SUPPLIERS = [
    {
        "name": "Claudia",
        "business_alias": "",
        "whatsapp_phone": "",
        "delivery_zone": "",
        "payment_terms": "",
        "priority": 100,
        "notes": "",
        "active": 1,
    },
    {
        "name": "Roger",
        "business_alias": "El negro manoplero",
        "whatsapp_phone": "",
        "delivery_zone": "",
        "payment_terms": "",
        "priority": 100,
        "notes": "",
        "active": 1,
    },
    {
        "name": "Rolando",
        "business_alias": "El q no corre, se manda",
        "whatsapp_phone": "",
        "delivery_zone": "",
        "payment_terms": "",
        "priority": 100,
        "notes": "",
        "active": 1,
    },
    {
        "name": "Yoan",
        "business_alias": "",
        "whatsapp_phone": "",
        "delivery_zone": "",
        "payment_terms": "",
        "priority": 100,
        "notes": "",
        "active": 1,
    },
]

GROUPS = [
    {
        "channel": "telegram",
        "name": "Canal Ventas Electrodomésticos",
        "status": "ACTIVE",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "",
        "sender": "userbot",
        "peer_id": -1004306069907,
    },
    {
        "channel": "telegram",
        "name": "Canal Ventas Alimentos",
        "status": "ACTIVE",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "",
        "sender": "userbot",
        "peer_id": -1003929132787,
    },
    {
        "channel": "telegram",
        "name": "Venta Electrodomésticos",
        "status": "ACTIVE",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "",
        "sender": "userbot",
        "peer_id": -5195658801,
    },
    {
        "channel": "telegram",
        "name": "Venta Motos E",
        "status": "ACTIVE",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "",
        "sender": "userbot",
        "peer_id": -5477392336,
    },
    {
        "channel": "telegram",
        "name": "Venta Motos C",
        "status": "ACTIVE",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "",
        "sender": "userbot",
        "peer_id": -5354386462,
    },
    {
        "channel": "telegram",
        "name": "Ventas Grupo Deshabilitado",
        "status": "PAUSED",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "Disabled group kept for filter testing",
        "sender": "userbot",
        "peer_id": None,
    },
    {
        "channel": "telegram",
        "name": "Ventas Canal Deshabilitado",
        "status": "PAUSED",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "Disabled channel kept for filter testing",
        "sender": "userbot",
        "peer_id": None,
    },
    {
        "channel": "telegram",
        "name": "Venta Motos E Playa",
        "status": "ACTIVE",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "Only posts in Playa (zone test)",
        "sender": "userbot",
        "peer_id": None,
    },
    {
        "channel": "telegram",
        "name": "Venta Snacks",
        "status": "ACTIVE",
        "max_posts_per_day": 20,
        "min_interval_s": 3,
        "max_interval_s": 8,
        "notes": "",
        "sender": "userbot",
        "peer_id": None,
    },
]

# --------------------------------------------------------------------------
# Group configuration.
#
# `ALL_ACTIVE_ZONES` means "Todas" (every active municipality). The rest are
# intentionally varied so the zone behavior can be tested: for example, Roger
# delivers in La Lisa / Marianao, so a Roger product should not be published in
# "Venta Motos E Playa" (which is Playa only).
# --------------------------------------------------------------------------

ALL_ACTIVE_ZONES = "__all_active__"

GROUP_CATEGORIES = [
    ("Canal Ventas Electrodomésticos", "tecnología"),
    ("Canal Ventas Electrodomésticos", "hogar"),
    ("Venta Electrodomésticos", "tecnología"),
    ("Canal Ventas Alimentos", "alimentos"),
    ("Venta Motos E", "motos_eléctricas"),
    ("Venta Motos E Playa", "motos_eléctricas"),
    ("Venta Motos C", "motos_combustión"),
    ("Venta Snacks", "snacks"),
]

GROUP_ZONES = {
    "Canal Ventas Electrodomésticos": ALL_ACTIVE_ZONES,
    "Canal Ventas Alimentos": ALL_ACTIVE_ZONES,
    "Venta Electrodomésticos": ALL_ACTIVE_ZONES,
    "Venta Motos C": ALL_ACTIVE_ZONES,
    "Venta Motos E": ["La Lisa", "Marianao"],
    "Venta Motos E Playa": ["Playa"],
    "Venta Snacks": ["Cerro", "Marianao", "Diez de Octubre"],
    "Ventas Grupo Deshabilitado": ["Playa"],
    "Ventas Canal Deshabilitado": ["Playa"],
}

SUPPLIER_DELIVERY_ZONES = [
    ("Claudia", "Playa"),
    ("Claudia", "Marianao"),
    ("Claudia", "Centro Habana"),
    ("Roger", "La Lisa"),
    ("Roger", "Marianao"),
    ("Rolando", "Playa"),
    ("Rolando", "La Lisa"),
    ("Rolando", "Boyeros"),
    ("Rolando", "San Miguel"),
    ("Rolando", "Arroyo Naranjo"),
    ("Rolando", "Cerro"),
    ("Rolando", "Cotorro"),
    ("Rolando", "Diez de Octubre"),
    ("Rolando", "Guanabacoa"),
    ("Rolando", "Marianao"),
    ("Rolando", "Plaza de la Revolución"),
    ("Rolando", "Regla"),
    ("Yoan", "Habana del Este"),
    ("Yoan", "Guanabacoa"),
    ("Yoan", "Regla"),
]

# --------------------------------------------------------------------------
# Categories. The original catalog is kept but reorganized so names match the
# products they group (`electrodomésticos` held laptops, which is confusing).
# --------------------------------------------------------------------------

CATEGORIES = [
    ("tecnología", 1),
    ("computación", 1),
    ("accesorios", 1),
    ("hogar", 1),
    ("snacks", 1),
    ("motos_eléctricas", 1),
    ("motos_combustión", 1),
    ("alimentos", 1),
]

# --------------------------------------------------------------------------
# Products: demo catalog only. The real catalog created by the user is not
# seeded anymore.
# `supplier` and `category` are matched by name.
# --------------------------------------------------------------------------

DEMO_PRODUCTS = [
    {
        "name": "Laptop Empresarial",
        "supplier": "Rolando",
        "category": "tecnología",
        "description": "Lenovo ThinkPad\ni5 11va gen\n16GB RAM\n512GB SSD",
        "price": 350.0,
        "currency": "USD",
        "commission": 25.0,
        "commission_currency": "USD",
        "stock": 4,
    },
    {
        "name": "Laptop Ultraligera",
        "supplier": "Rolando",
        "category": "tecnología",
        "description": "ASUS ZenBook\nRyzen 7\n16GB RAM\n1TB SSD",
        "price": 720.0,
        "currency": "USD",
        "commission": 35.0,
        "commission_currency": "USD",
        "stock": 2,
    },
    {
        "name": "Monitor LED 24",
        "supplier": "Rolando",
        "category": "tecnología",
        "description": 'Monitor LED 24"\nFull HD 75Hz\nHDMI + VGA',
        "price": 180.0,
        "currency": "USD",
        "commission": 15.0,
        "commission_currency": "USD",
        "stock": 6,
    },
    {
        "name": "Tablet Android 10",
        "supplier": "Rolando",
        "category": "tecnología",
        "description": "Tablet 10 pulgadas\n4GB RAM\n64GB almacenamiento",
        "price": 220.0,
        "currency": "USD",
        "commission": 20.0,
        "commission_currency": "USD",
        "stock": 5,
    },
    {
        "name": "Aspiradora Portátil",
        "supplier": "Rolando",
        "category": "hogar",
        "description": "Aspiradora portátil\n1200W\nfiltro HEPA",
        "price": 95.0,
        "currency": "USD",
        "commission": 10.0,
        "commission_currency": "USD",
        "stock": 8,
    },
    {
        "name": "Freidora de Aire",
        "supplier": "Rolando",
        "category": "hogar",
        "description": "Freidora de aire 5L\n1500W",
        "price": 130.0,
        "currency": "USD",
        "commission": 12.0,
        "commission_currency": "USD",
        "stock": 5,
    },
    {
        "name": "Moto Eléctrica Urban",
        "supplier": "Roger",
        "category": "motos_eléctricas",
        "description": "Motorina Urban\n72V x 32A\nautonomía 80 km",
        "price": 1350.0,
        "currency": "USD",
        "commission": 45.0,
        "commission_currency": "USD",
        "stock": 4,
    },
    {
        "name": "Moto Eléctrica Cargo",
        "supplier": "Roger",
        "category": "motos_eléctricas",
        "description": "Motorina Cargo\n72V x 50A\ncaja trasera 200 kg",
        "price": 2500.0,
        "currency": "USD",
        "commission": 60.0,
        "commission_currency": "USD",
        "stock": 2,
    },
    {
        "name": "Moto Eléctrica Sport",
        "supplier": "Roger",
        "category": "motos_eléctricas",
        "description": "Motorina Sport\n72V x 40A",
        "price": 1800.0,
        "currency": "USD",
        "commission": 50.0,
        "commission_currency": "USD",
        "stock": 3,
    },
    {
        "name": "Moto Combustión Yamaha 150",
        "supplier": "Roger",
        "category": "motos_combustión",
        "description": "Yamaha YBR 150\n4 tiempos\narranque eléctrico",
        "price": 2100.0,
        "currency": "USD",
        "commission": 55.0,
        "commission_currency": "USD",
        "stock": 3,
    },
    {
        "name": "Moto Combustión Haojue 200",
        "supplier": "Roger",
        "category": "motos_combustión",
        "description": "Haojue 200CC\n4 tiempos",
        "price": 2300.0,
        "currency": "USD",
        "commission": 60.0,
        "commission_currency": "USD",
        "stock": 2,
    },
    {
        "name": "Pan con Croqueta",
        "supplier": "Roger",
        "category": "alimentos",
        "description": "Pan con croqueta de jamón\nhecho al momento",
        "price": 300.0,
        "currency": "CUP",
        "commission": 30.0,
        "commission_currency": "CUP",
        "stock": 150,
    },
    {
        "name": "Café con Leche",
        "supplier": "Roger",
        "category": "alimentos",
        "description": "Café con leche\nvaso 8 oz",
        "price": 150.0,
        "currency": "CUP",
        "commission": 15.0,
        "commission_currency": "CUP",
        "stock": 300,
    },
    {
        "name": "Combo Desayuno",
        "supplier": "Roger",
        "category": "alimentos",
        "description": "Café con leche + pan con tortilla",
        "price": 450.0,
        "currency": "CUP",
        "commission": 40.0,
        "commission_currency": "CUP",
        "stock": 80,
    },
    {
        "name": "Pizza Personal",
        "supplier": "Roger",
        "category": "alimentos",
        "description": "Pizza personal\nextra queso",
        "price": 350.0,
        "currency": "CUP",
        "commission": 35.0,
        "commission_currency": "CUP",
        "stock": 60,
    },
    {
        "name": "Jugo Natural",
        "supplier": "Roger",
        "category": "alimentos",
        "description": "Jugo natural 12 oz",
        "price": 200.0,
        "currency": "CUP",
        "commission": 20.0,
        "commission_currency": "CUP",
        "stock": 120,
    },
    {
        "name": "PC de Escritorio Ryzen 5",
        "supplier": "Rolando",
        "category": "computación",
        "description": "Ryzen 5 5600G\n16GB RAM\n512GB SSD",
        "price": 480.0,
        "currency": "USD",
        "commission": 40.0,
        "commission_currency": "USD",
        "stock": 3,
    },
    {
        "name": "Impresora Multifuncional",
        "supplier": "Rolando",
        "category": "computación",
        "description": "Impresora multifuncional\nWiFi + USB",
        "price": 210.0,
        "currency": "USD",
        "commission": 18.0,
        "commission_currency": "USD",
        "stock": 4,
    },
    {
        "name": "Mouse Inalámbrico",
        "supplier": "Rolando",
        "category": "accesorios",
        "description": "Mouse inalámbrico 2.4GHz\nreceptor USB",
        "price": 15.0,
        "currency": "USD",
        "commission": 3.0,
        "commission_currency": "USD",
        "stock": 25,
    },
    {
        "name": "Teclado Mecánico",
        "supplier": "Rolando",
        "category": "accesorios",
        "description": "Teclado mecánico RGB\nswitch azul",
        "price": 45.0,
        "currency": "USD",
        "commission": 6.0,
        "commission_currency": "USD",
        "stock": 15,
    },
    {
        "name": "Audífonos Bluetooth",
        "supplier": "Rolando",
        "category": "accesorios",
        "description": "Audífonos Bluetooth 5.3",
        "price": 35.0,
        "currency": "USD",
        "commission": 5.0,
        "commission_currency": "USD",
        "stock": 20,
    },
    {
        "name": "Papas Fritas",
        "supplier": "Roger",
        "category": "snacks",
        "description": "Papas fritas\nbolsa 150 g",
        "price": 180.0,
        "currency": "CUP",
        "commission": 18.0,
        "commission_currency": "CUP",
        "stock": 200,
    },
    {
        "name": "Refresco Cola",
        "supplier": "Roger",
        "category": "snacks",
        "description": "Refresco de cola\nlata 355 ml",
        "price": 220.0,
        "currency": "CUP",
        "commission": 22.0,
        "commission_currency": "CUP",
        "stock": 180,
    },
    {
        "name": "Chocolate con Leche",
        "supplier": "Roger",
        "category": "snacks",
        "description": "Chocolate con leche\nbarra 90 g",
        "price": 260.0,
        "currency": "CUP",
        "commission": 26.0,
        "commission_currency": "CUP",
        "stock": 120,
    },
    {
        "name": "Maní Salado",
        "supplier": "Roger",
        "category": "snacks",
        "description": "Maní salado\nbolsa 100 g",
        "price": 150.0,
        "currency": "CUP",
        "commission": 15.0,
        "commission_currency": "CUP",
        "stock": 150,
    },
    # --- Scenario data: new suppliers -------------------------------------
    {
        "name": "Laptop Gamer RTX 3050",
        "supplier": "Claudia",
        "category": "tecnología",
        "description": "Laptop gaming\nRTX 3050\n16GB RAM\n512GB SSD",
        "price": 850.0,
        "currency": "USD",
        "commission": 50.0,
        "commission_currency": "USD",
        "stock": 2,
    },
    {
        "name": "Cafetera Express",
        "supplier": "Claudia",
        "category": "hogar",
        "description": "Cafetera express\n15 bares\napagado automático",
        "price": 160.0,
        "currency": "USD",
        "commission": 15.0,
        "commission_currency": "USD",
        "stock": 6,
    },
    {
        "name": "Power Bank 20000",
        "supplier": "Claudia",
        "category": "accesorios",
        "description": "Power bank 20000 mAh\ncarga rápida 22.5W",
        "price": 40.0,
        "currency": "USD",
        "commission": 4.0,
        "commission_currency": "USD",
        "stock": 0,
        "status": "OUT_OF_STOCK",
    },
    {
        "name": "Cable HDMI 2 m",
        "supplier": "Claudia",
        "category": "accesorios",
        "description": "Cable HDMI 2.1\n2 metros\n4K 120Hz",
        "price": 8.0,
        "currency": "USD",
        "commission": 1.0,
        "commission_currency": "USD",
        "stock": 40,
    },
    {
        "name": "Moto Eléctrica Delivery",
        "supplier": "Yoan",
        "category": "motos_eléctricas",
        "description": "Motorina Delivery\n72V x 40A\ncaja térmica",
        "price": 1600.0,
        "currency": "USD",
        "commission": 50.0,
        "commission_currency": "USD",
        "stock": 3,
    },
    {
        "name": "Moto Combustión Zontes 250",
        "supplier": "Yoan",
        "category": "motos_combustión",
        "description": "Zontes 250CC\ninyección electrónica",
        "price": 2600.0,
        "currency": "USD",
        "commission": 70.0,
        "commission_currency": "USD",
        "stock": 2,
    },
    {
        "name": "Combo Personalizado",
        "supplier": "Rolando",
        "category": None,
        "description": "Combo armado a pedido\ncliente elige los productos",
        "price": 60.0,
        "currency": "USD",
        "commission": 6.0,
        "commission_currency": "USD",
        "stock": 10,
    },
    # --- Real test 2026-11-12: widen the thin catalogs ---------------------
    # Claudia and Yoan had only 4 and 2 products. These additions also cover
    # cases the seed did not: low stock (1), a PAUSED product and two names
    # that are easy to confuse ("Urban" vs "Urban Plus").
    {
        "name": "Microondas 20L",
        "supplier": "Claudia",
        "category": "hogar",
        "description": "Microondas 20 litros\n700W\n5 niveles de potencia",
        "price": 140.0,
        "currency": "USD",
        "commission": 14.0,
        "commission_currency": "USD",
        "stock": 3,
    },
    {
        "name": "Ventilador de Torre",
        "supplier": "Claudia",
        "category": "hogar",
        "description": "Ventilador de torre\noscilante\n3 velocidades",
        "price": 55.0,
        "currency": "USD",
        "commission": 5.0,
        "commission_currency": "USD",
        "stock": 6,
    },
    {
        "name": "Cargador Rápido 20W",
        "supplier": "Claudia",
        "category": "accesorios",
        "description": "Cargador rápido 20W\nUSB-C\ncompatible con iPhone y Android",
        "price": 18.0,
        "currency": "USD",
        "commission": 2.0,
        "commission_currency": "USD",
        "stock": 30,
    },
    {
        "name": "Moto Eléctrica City",
        "supplier": "Yoan",
        "category": "motos_eléctricas",
        "description": "Motorina City\n60V x 20A\nautonomía 45 km",
        "price": 1200.0,
        "currency": "USD",
        "commission": 40.0,
        "commission_currency": "USD",
        "stock": 3,
    },
    {
        "name": "Batería para Moto Eléctrica",
        "supplier": "Yoan",
        "category": "accesorios",
        "description": "Batería 60V x 20A\npara moto eléctrica",
        "price": 250.0,
        "currency": "USD",
        "commission": 20.0,
        "commission_currency": "USD",
        "stock": 5,
    },
    {
        "name": "Casco para Moto",
        "supplier": "Yoan",
        "category": "accesorios",
        "description": "Casco certificado\ntalla ajustable",
        "price": 40.0,
        "currency": "USD",
        "commission": 4.0,
        "commission_currency": "USD",
        "stock": 10,
    },
    {
        "name": "Laptop Gamer RTX 4060",
        "supplier": "Rolando",
        "category": "tecnología",
        "description": "Laptop gaming\nRTX 4060\n16GB RAM\n1TB SSD",
        "price": 1100.0,
        "currency": "USD",
        "commission": 60.0,
        "commission_currency": "USD",
        "stock": 1,
    },
    {
        "name": "Router WiFi 6",
        "supplier": "Rolando",
        "category": "tecnología",
        "description": "Router WiFi 6\ndual band\n4 antenas",
        "price": 90.0,
        "currency": "USD",
        "commission": 9.0,
        "commission_currency": "USD",
        "stock": 4,
        "status": "PAUSED",
    },
    {
        "name": "Moto Eléctrica Urban Plus",
        "supplier": "Roger",
        "category": "motos_eléctricas",
        "description": "Motorina Urban Plus\n72V x 35A\nautonomía 95 km",
        "price": 1500.0,
        "currency": "USD",
        "commission": 50.0,
        "commission_currency": "USD",
        "stock": 2,
    },
]

# --------------------------------------------------------------------------
# Broadcasts. `groups: None` uses automatic targeting (product category).
# A list of group names creates explicit `broadcast_group_links`.
# --------------------------------------------------------------------------

TEMPLATE = (
    "{producto} disponible\n"
    "\n"
    "Precio: {precio} {moneda}\n"
    "Proveedor: {proveedor}\n"
    "Categoría: {categoria}\n"
    "\n"
    "Escríbenos para más información."
)

# Exercises spintax, the `{zona}` token (supplier delivery zones) and `{id}`
# (when present, the scheduler does not append the product id).
ZONE_TEMPLATE = (
    "{Hola|Oferta} {producto} disponible en {zona}\n"
    "\n"
    "Precio: {precio} {moneda}\n"
    "Proveedor: {proveedor}\n"
    "ID: {id}"
)

DEMO_BROADCASTS = [
    {"product": "Laptop Empresarial", "groups": None},
    {
        "product": "Monitor LED 24",
        "groups": ["Canal Ventas Electrodomésticos", "Venta Electrodomésticos"],
    },
    {"product": "Moto Eléctrica Urban", "groups": None},
    {"product": "Moto Eléctrica Cargo", "groups": ["Venta Motos E"]},
    {"product": "Moto Combustión Yamaha 150", "groups": ["Venta Motos C"]},
    {"product": "Combo Desayuno", "groups": None},
    {"product": "Café con Leche", "groups": ["Canal Ventas Alimentos"]},
    {"product": "PC de Escritorio Ryzen 5", "groups": ["Canal Ventas Electrodomésticos"]},
    {"product": "Aspiradora Portátil", "groups": None},
    {"product": "Moto Eléctrica Sport", "groups": ["Venta Motos E"]},
    {"product": "Pizza Personal", "groups": None},
    {"product": "Teclado Mecánico", "groups": ["Canal Ventas Electrodomésticos"]},
    {"product": "Jugo Natural", "groups": ["Canal Ventas Alimentos"]},
    {"product": "Freidora de Aire", "groups": None},
    {"product": "Papas Fritas", "groups": None},
    {"product": "Refresco Cola", "groups": ["Venta Snacks"]},
    {
        "product": "Impresora Multifuncional",
        "groups": ["Canal Ventas Electrodomésticos"],
    },
    # --- Scenario data: targeting and publishing edge cases ---------------
    # Auto + category with no eligible group -> FAILED on the first tick.
    {"product": "Cable HDMI 2 m", "groups": None},
    # Auto + product without category -> only groups without restrictions.
    {"product": "Combo Personalizado", "groups": None},
    # Manual + out-of-stock product -> publishing is not blocked by status.
    {
        "product": "Power Bank 20000",
        "groups": ["Canal Ventas Electrodomésticos"],
    },
    # Manual + active and paused groups -> the paused one is ignored.
    {
        "product": "Cafetera Express",
        "groups": ["Canal Ventas Electrodomésticos", "Ventas Grupo Deshabilitado"],
    },
    # Manual + only a paused group -> FAILED on the first tick.
    {"product": "Moto Eléctrica Delivery", "groups": ["Ventas Canal Deshabilitado"]},
    # Auto + spintax template with {zona} and {id}.
    {
        "product": "Laptop Gamer RTX 3050",
        "groups": None,
        "template": ZONE_TEMPLATE,
    },
    # Manual + product category not allowed by the group -> manual overrides.
    {
        "product": "Moto Combustión Zontes 250",
        "groups": ["Venta Electrodomésticos"],
    },
    # --- Real test 2026-11-12: campaigns for the new products --------------
    # Auto targeting by category (hogar / tecnología).
    {"product": "Microondas 20L", "groups": None},
    {"product": "Ventilador de Torre", "groups": None},
    {"product": "Laptop Gamer RTX 4060", "groups": None},
    # PAUSED product: it still publishes, but must not be sold. The catalog
    # rules are what stop a sale, so the campaign is useful to test that path.
    {"product": "Router WiFi 6", "groups": None},
    # No group is tagged "accesorios", so these are manual in all-zone groups.
    {
        "product": "Cargador Rápido 20W",
        "groups": ["Canal Ventas Electrodomésticos", "Venta Electrodomésticos"],
    },
    {"product": "Casco para Moto", "groups": ["Venta Electrodomésticos"]},
    # Yoan delivers in Habana del Este / Guanabacoa / Regla, so the moto
    # groups (La Lisa / Marianao) would not match; manual all-zone group.
    {"product": "Moto Eléctrica City", "groups": ["Venta Electrodomésticos"]},
    {
        "product": "Batería para Moto Eléctrica",
        "groups": ["Venta Electrodomésticos"],
    },
    # Roger delivers in La Lisa / Marianao, same zones as "Venta Motos E".
    {"product": "Moto Eléctrica Urban Plus", "groups": ["Venta Motos E"]},
]

DISABLED_BROADCASTS = [
    {"product": "Mouse Inalámbrico"},
]

# --------------------------------------------------------------------------
# Helpers: natural-key lookups with support for --dry-run (negative ids).
# --------------------------------------------------------------------------

_PENDING: dict[str, dict[str, int]] = {
    "zones": {},
    "categories": {},
    "suppliers": {},
    "groups": {},
    "products": {},
}


def _pending_id(kind: str, key: str) -> int:
    bucket = _PENDING[kind]
    if key not in bucket:
        bucket[key] = -len(bucket) - 1
    return bucket[key]


def _lookup_id(conn, table: str, column: str, value, kind: str) -> int | None:
    row = conn.execute(f"SELECT id FROM {table} WHERE {column} = ?", (value,)).fetchone()
    if row is not None:
        return row["id"]
    return _PENDING[kind].get(value)


def ensure_zone(conn, name: str, active: int, dry_run: bool) -> tuple[int, bool]:
    existing = _lookup_id(conn, "zones", "name", name, "zones")
    if existing is not None:
        return existing, False
    if dry_run:
        return _pending_id("zones", name), True
    cur = conn.execute("INSERT INTO zones (name, active) VALUES (?, ?)", (name, active))
    _PENDING["zones"][name] = cur.lastrowid
    return cur.lastrowid, True


def ensure_category(conn, name: str, active: int, dry_run: bool) -> tuple[int, bool]:
    existing = _lookup_id(conn, "categories", "name", name, "categories")
    if existing is not None:
        return existing, False
    if dry_run:
        return _pending_id("categories", name), True
    cur = conn.execute(
        "INSERT INTO categories (name, active) VALUES (?, ?)", (name, active)
    )
    _PENDING["categories"][name] = cur.lastrowid
    return cur.lastrowid, True


def ensure_supplier(conn, spec: dict, dry_run: bool) -> tuple[int, bool]:
    existing = _lookup_id(conn, "suppliers", "name", spec["name"], "suppliers")
    if existing is not None:
        return existing, False
    if dry_run:
        return _pending_id("suppliers", spec["name"]), True
    cur = conn.execute(
        """INSERT INTO suppliers
               (name, business_alias, whatsapp_phone, telegram_id, delivery_zone,
                payment_terms, priority, notes, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            spec["name"],
            spec["business_alias"],
            spec["whatsapp_phone"],
            spec["telegram_id"],
            spec["delivery_zone"],
            spec["payment_terms"],
            spec["priority"],
            spec["notes"],
            spec["active"],
        ),
    )
    _PENDING["suppliers"][spec["name"]] = cur.lastrowid
    return cur.lastrowid, True


def ensure_group(conn, spec: dict, dry_run: bool) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT id FROM broadcast_groups WHERE channel = ? AND target = ?",
        (spec["channel"], spec["target"]),
    ).fetchone()
    if row is not None:
        _PENDING["groups"][spec["name"]] = row["id"]
        return row["id"], False
    if dry_run:
        group_id = _pending_id("groups", spec["name"])
        return group_id, True
    cur = conn.execute(
        """INSERT INTO broadcast_groups
               (channel, target, name, status, max_posts_per_day, min_interval_s,
                max_interval_s, notes, sender, peer_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            spec["channel"],
            spec["target"],
            spec["name"],
            spec["status"],
            spec["max_posts_per_day"],
            spec["min_interval_s"],
            spec["max_interval_s"],
            spec["notes"],
            spec["sender"],
            spec["peer_id"],
        ),
    )
    _PENDING["groups"][spec["name"]] = cur.lastrowid
    return cur.lastrowid, True


def ensure_product(conn, spec: dict, dry_run: bool) -> tuple[int | None, bool]:
    existing = _lookup_id(conn, "products", "name", spec["name"], "products")
    if existing is not None:
        return existing, False

    supplier_id = _lookup_id(conn, "suppliers", "name", spec["supplier"], "suppliers")
    if supplier_id is None:
        print(f"  Skipping {spec['name']}: supplier {spec['supplier']!r} not found")
        return None, False

    category_id = None
    if spec.get("category"):
        category_id = _lookup_id(
            conn, "categories", "name", spec["category"], "categories"
        )
        if category_id is None:
            print(
                f"  Skipping {spec['name']}: category {spec['category']!r} not found"
            )
            return None, False

    if dry_run:
        product_id = _pending_id("products", spec["name"])
    else:
        cur = conn.execute(
            """INSERT INTO products
                   (supplier_id, category_id, name, description, price, currency,
                    commission, commission_currency, stock, status, specifications_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
            (
                supplier_id,
                category_id,
                spec["name"],
                spec["description"],
                spec["price"],
                spec["currency"],
                spec["commission"],
                spec["commission_currency"],
                spec["stock"],
                spec.get("status", "ACTIVE"),
            ),
        )
        product_id = cur.lastrowid
        _PENDING["products"][spec["name"]] = product_id

    print(
        f"  + product {spec['name']} "
        f"(category={spec.get('category')}, supplier={spec['supplier']})"
    )
    return product_id, True


def ensure_mapping(
    conn, table: str, left: str, left_id: int, right: str, right_id: int, dry_run: bool
) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {left} = ? AND {right} = ?", (left_id, right_id)
    ).fetchone()
    if row is not None:
        return False
    if not dry_run:
        conn.execute(
            f"INSERT INTO {table} ({left}, {right}) VALUES (?, ?)", (left_id, right_id)
        )
    return True


def _template_for(product_name: str) -> str:
    return TEMPLATE.replace("{producto}", product_name, 1)


def _next_send_at(mode: str, index: int) -> str | None:
    if mode == "now":
        return None
    now = datetime.now(timezone.utc)
    return (now + timedelta(hours=4 + index)).strftime("%Y-%m-%d %H:%M:%S")


def ensure_broadcast(
    conn,
    spec: dict,
    status: str,
    mode: str,
    index: int,
    dry_run: bool,
    *,
    enabled: int = 1,
) -> tuple[int | None, bool, int]:
    template = spec.get("template") or _template_for(spec["product"])
    row = conn.execute("SELECT id FROM broadcasts WHERE template = ?", (template,)).fetchone()

    product_id = _lookup_id(conn, "products", "name", spec["product"], "products")
    if product_id is None:
        print(f"  Skipping broadcast {spec['product']!r}: product not found")
        return None, False, 0

    group_ids = []
    for group_name in spec.get("groups") or []:
        group_id = _lookup_id(conn, "broadcast_groups", "name", group_name, "groups")
        if group_id is None:
            print(
                f"  Skipping broadcast {spec['product']!r}: group {group_name!r} not found"
            )
            return None, False, 0
        group_ids.append(group_id)

    if row is not None:
        broadcast_id = row["id"]
        created = False
    elif dry_run:
        broadcast_id = -index - 1
        created = True
    else:
        next_send_at = (
            _next_send_at(mode, index) if status == "SCHEDULED" and enabled else None
        )
        cur = conn.execute(
            """INSERT INTO broadcasts
                   (channel, template, product_id, status, enabled, next_send_at)
               VALUES ('telegram', ?, ?, ?, ?, ?)""",
            (template, product_id, status, enabled, next_send_at),
        )
        broadcast_id = cur.lastrowid
        created = True

    links = 0
    for group_id in group_ids:
        if ensure_mapping(
            conn,
            "broadcast_group_links",
            "broadcast_id",
            broadcast_id,
            "group_id",
            group_id,
            dry_run,
        ):
            links += 1

    if created:
        mode_label = "auto" if not group_ids else ",".join(
            str(gid) for gid in group_ids
        )
        print(
            f"  + broadcast {spec['product']} "
            f"(status={status}, enabled={enabled}, groups={mode_label})"
        )
    return broadcast_id, created, links


# --------------------------------------------------------------------------
# Local real-data overrides (never versioned).
# --------------------------------------------------------------------------


class SeedError(RuntimeError):
    """The local real-data file is missing or incomplete."""


def load_real_data(path: Path) -> dict:
    if not path.is_file():
        raise SeedError(
            f"Real data file not found: {path}\n"
            "Create it with the suppliers' telegram_id and the groups' target. "
            "The expected schema is documented in cuadra_doc/09_seed_demo_data.md."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SeedError(f"Invalid JSON in {path}: {exc}") from exc
    for section in ("suppliers", "groups"):
        if not isinstance(data.get(section), dict):
            raise SeedError(f"{path}: missing or invalid '{section}' object")
    return data


def apply_real_data(suppliers: list[dict], groups: list[dict], real: dict, source: Path) -> None:
    """Copy the sensitive values into the in-memory specs, failing on gaps.

    Matching is by natural key (supplier/group name), so the seed never needs
    placeholder identifiers and the JSON stays easy to read.
    """
    missing: list[str] = []

    for spec in suppliers:
        entry = real["suppliers"].get(spec["name"]) or {}
        telegram_id = entry.get("telegram_id")
        if not telegram_id:
            missing.append(f"suppliers[{spec['name']!r}].telegram_id")
        else:
            spec["telegram_id"] = telegram_id

    for spec in groups:
        entry = real["groups"].get(spec["name"]) or {}
        target = entry.get("target")
        if not target:
            missing.append(f"groups[{spec['name']!r}].target")
        else:
            spec["target"] = target

    if missing:
        raise SeedError(f"{source} is missing: " + ", ".join(missing))

    known_suppliers = {spec["name"] for spec in suppliers}
    known_groups = {spec["name"] for spec in groups}
    for name in real["suppliers"]:
        if name not in known_suppliers:
            print(f"  Warning: unknown supplier {name!r} in {source}")
    for name in real["groups"]:
        if name not in known_groups:
            print(f"  Warning: unknown group {name!r} in {source}")


# --------------------------------------------------------------------------
# Main seeding flow.
# --------------------------------------------------------------------------


def compute_schedule_mode(requested: str, existing_broadcasts: int) -> str:
    if requested != "auto":
        return requested
    return "now" if existing_broadcasts == 0 else "future"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed the full basic dataset (real + demo) for testing"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be created without writing"
    )
    parser.add_argument(
        "--schedule",
        choices=("auto", "now", "future"),
        default="auto",
        help=(
            "auto: due now on an empty campaign table, staggered future otherwise "
            "(default); now: always due immediately; future: always staggered"
        ),
    )
    parser.add_argument(
        "--real-data",
        type=Path,
        default=REAL_DATA_PATH,
        help=(
            "JSON file with the sensitive values (supplier telegram_id and group "
            f"target). Default: {REAL_DATA_PATH}"
        ),
    )
    args = parser.parse_args()

    try:
        real_data = load_real_data(args.real_data)
        apply_real_data(SUPPLIERS, GROUPS, real_data, args.real_data)
    except SeedError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Real data loaded from {args.real_data}")

    conn = db.connect()
    try:
        applied = db.apply_migrations(conn)
        if applied:
            print(f"Applied {len(applied)} migration(s) to the database")

        counts = {
            "zones": 0,
            "categories": 0,
            "suppliers": 0,
            "groups": 0,
            "group_categories": 0,
            "group_zones": 0,
            "supplier_delivery_zones": 0,
            "products": 0,
            "broadcasts": 0,
            "broadcast_group_links": 0,
        }

        print("Zones:")
        for name, active in ZONES:
            _, created = ensure_zone(conn, name, active, args.dry_run)
            counts["zones"] += int(created)

        print("Categories:")
        for name, active in CATEGORIES:
            _, created = ensure_category(conn, name, active, args.dry_run)
            counts["categories"] += int(created)

        print("Suppliers (TCPs):")
        for spec in SUPPLIERS:
            _, created = ensure_supplier(conn, spec, args.dry_run)
            counts["suppliers"] += int(created)

        print("Broadcast groups:")
        for spec in GROUPS:
            _, created = ensure_group(conn, spec, args.dry_run)
            counts["groups"] += int(created)

        print("Group categories:")
        for group_name, category_name in GROUP_CATEGORIES:
            group_id = _lookup_id(conn, "broadcast_groups", "name", group_name, "groups")
            category_id = _lookup_id(conn, "categories", "name", category_name, "categories")
            if group_id is None or category_id is None:
                print(f"  Skipping mapping {group_name} -> {category_name}: missing row")
                continue
            if ensure_mapping(
                conn,
                "group_categories",
                "group_id",
                group_id,
                "category_id",
                category_id,
                args.dry_run,
            ):
                counts["group_categories"] += 1

        print("Group zones:")
        all_active_zones = [name for name, active in ZONES if active]
        for group_name, zone_names in GROUP_ZONES.items():
            group_id = _lookup_id(conn, "broadcast_groups", "name", group_name, "groups")
            if group_id is None:
                print(f"  Skipping zone mapping for {group_name}: group not found")
                continue
            selected = (
                all_active_zones if zone_names == ALL_ACTIVE_ZONES else zone_names
            )
            for zone_name in selected:
                zone_id = _lookup_id(conn, "zones", "name", zone_name, "zones")
                if zone_id is None:
                    print(f"  Skipping mapping {group_name} -> {zone_name}: missing row")
                    continue
                if ensure_mapping(
                    conn,
                    "group_zones",
                    "group_id",
                    group_id,
                    "zone_id",
                    zone_id,
                    args.dry_run,
                ):
                    counts["group_zones"] += 1

        print("Supplier delivery zones:")
        for supplier_name, zone_name in SUPPLIER_DELIVERY_ZONES:
            supplier_id = _lookup_id(
                conn, "suppliers", "name", supplier_name, "suppliers"
            )
            zone_id = _lookup_id(conn, "zones", "name", zone_name, "zones")
            if supplier_id is None or zone_id is None:
                print(f"  Skipping mapping {supplier_name} -> {zone_name}: missing row")
                continue
            if ensure_mapping(
                conn,
                "supplier_delivery_zones",
                "supplier_id",
                supplier_id,
                "zone_id",
                zone_id,
                args.dry_run,
            ):
                counts["supplier_delivery_zones"] += 1

        print("Products:")
        for spec in DEMO_PRODUCTS:
            _, created = ensure_product(conn, spec, args.dry_run)
            counts["products"] += int(created)

        existing_broadcasts = conn.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0]
        mode = compute_schedule_mode(args.schedule, existing_broadcasts)
        print(f"Broadcasts (schedule={mode}):")

        index = 0
        for spec in DEMO_BROADCASTS:
            _, created, links = ensure_broadcast(
                conn, spec, "SCHEDULED", mode, index, args.dry_run
            )
            counts["broadcasts"] += int(created)
            counts["broadcast_group_links"] += links
            index += 1

        for spec in DISABLED_BROADCASTS:
            _, created, links = ensure_broadcast(
                conn, spec, "SCHEDULED", mode, index, args.dry_run, enabled=0
            )
            counts["broadcasts"] += int(created)
            counts["broadcast_group_links"] += links
            index += 1

        if args.dry_run:
            conn.rollback()
            print("Dry run, nothing written.")
        else:
            conn.commit()
            print("Seed completed.")

        print(
            "Created: "
            + ", ".join(f"{name}={value}" for name, value in counts.items())
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - report and roll back
        conn.rollback()
        print(f"Error while seeding data (rolled back): {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

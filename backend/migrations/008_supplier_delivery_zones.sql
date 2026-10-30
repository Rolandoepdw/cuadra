CREATE TABLE supplier_delivery_zones (
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    zone_id     INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    PRIMARY KEY (supplier_id, zone_id)
);


-- **Instructions:**
-- 1. Save this code block as `schema.sql`.
-- 2. Open your TiDB Cloud Console.
-- 3. Import or copy-paste this entire block into the SQL Editor and run it.


/*
   TiDB Schema for Antigravity Agent Demo
   --------------------------------------
   Purpose: Supports a Sales Engineering Agent capable of:
   1. Real-time Analytics (HTAP)
   2. Semantic Search (Vector)
   3. Stateful Conversation (Episodic Memory)
*/

-- ==========================================
-- 1. FACT MEMORY (Relational Data)
-- ==========================================

-- Customers: Who are we selling to?
CREATE TABLE IF NOT EXISTS customers (
    customer_id INT AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha', -- tenant isolation (fact-layer parity)
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100),
    region VARCHAR(50), -- e.g., 'EMEA', 'NA', 'APAC'
    vip_status BOOLEAN DEFAULT FALSE,
    signup_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_customers_tenant (tenant_id)
);

-- Products: The catalog with Vector Embeddings for Semantic Search
-- Note: VECTOR(384) matches the 'all-MiniLM-L6-v2' model output
CREATE TABLE IF NOT EXISTS products (
    product_id INT AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha', -- tenant isolation
    name VARCHAR(100) NOT NULL,
    category VARCHAR(50),
    price DECIMAL(10, 2),
    description TEXT,
    embedding VECTOR(384),
    INDEX idx_products_tenant (tenant_id)
);

-- Orders: The Transactional Link (Fact Table)
CREATE TABLE IF NOT EXISTS orders (
    order_id INT AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha', -- tenant isolation
    customer_id INT,
    product_id INT,
    quantity INT,
    amount DECIMAL(10,2),
    ip_address VARCHAR(45),
    country VARCHAR(50),
    status ENUM('pending', 'flagged', 'cleared', 'fraudulent') DEFAULT 'pending',
    flagged_reason TEXT,
    order_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_orders_tenant (tenant_id),
    INDEX idx_orders_tenant_ip (tenant_id, ip_address),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

-- ==========================================
-- 2. SEMANTIC MEMORY (Knowledge Base)
-- ==========================================

-- Sales Knowledge: Business rules and policies for RAG
CREATE TABLE IF NOT EXISTS sales_knowledge (
    doc_id INT AUTO_INCREMENT PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha', -- tenant isolation
    content TEXT,        -- e.g., "Gaming Laptops have a 14-day return limit."
    category VARCHAR(50), -- e.g., 'Returns', 'Shipping'
    embedding VECTOR(384),
    metadata JSON,       -- e.g., {"priority": "high", "tags": ["laptop", "strict"]}
    INDEX idx_sales_knowledge_tenant (tenant_id)
);

-- ==========================================
-- 3. EPISODIC MEMORY (Agent State)
-- ==========================================

-- Sessions: Tracks distinct user conversations
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id VARCHAR(36) PRIMARY KEY, -- UUID
    user_id VARCHAR(100),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    metadata JSON -- Flexible context like {"sentiment": "positive"}
);

-- Chat History: The "Black Box" recorder for Root Cause Analysis (RCA)
CREATE TABLE IF NOT EXISTS chat_history (
    message_id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(36),
    role ENUM('user', 'assistant', 'system', 'tool'), 
    content TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    metadata JSON, -- Stores tool usage: {"tool": "Vector Search"}
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
);

-- ==========================================
-- 4. OPTIMIZATIONS (The "Secret Sauce")
-- ==========================================

/* 
   A. Enable TiFlash (HTAP) 
   This pushes data to the Columnar Engine for real-time analytics 
   and accelerates Vector Search scans.
*/
ALTER TABLE customers SET TIFLASH REPLICA 1;
ALTER TABLE products SET TIFLASH REPLICA 1;
ALTER TABLE orders SET TIFLASH REPLICA 1;
ALTER TABLE sales_knowledge SET TIFLASH REPLICA 1;
ALTER TABLE chat_history SET TIFLASH REPLICA 1; -- Enables analytics on user questions

/* 
   B. Create Vector Indexes (HNSW)
   This enables Approximate Nearest Neighbor (ANN) search for low latency.
   Note: Without this, vector search is a full table scan (slower but accurate).
*/
-- Index for Product Catalog
ALTER TABLE products DROP INDEX IF EXISTS idx_prod_embedding;
ALTER TABLE products ADD VECTOR INDEX idx_prod_embedding ((VEC_L2_DISTANCE(embedding)));

-- Index for Knowledge Base
ALTER TABLE sales_knowledge DROP INDEX IF EXISTS idx_know_embedding;
ALTER TABLE sales_knowledge ADD VECTOR INDEX idx_know_embedding ((VEC_L2_DISTANCE(embedding)));

-- ==========================================
-- 5. REVIEWS (Operational ML Data)
-- ==========================================

/*
   Stores product and service reviews written by customers.
   - Sentiment score + label pre-computed at write time (simulates edge inference)
   - Embedding enables semantic search: "find reviews mentioning slow delivery"
   - TiFlash replica enables real-time aggregate analytics (AVG rating, sentiment trend)
     directly on operational data — no ETL, no data warehouse needed.
*/
CREATE TABLE IF NOT EXISTS reviews (
    review_id   INT AUTO_INCREMENT PRIMARY KEY,
    tenant_id   VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha', -- tenant isolation
    customer_id INT NOT NULL,
    product_id  INT NULL,                         -- NULL for service/experience reviews
    review_type ENUM('product', 'service') DEFAULT 'product',
    rating      TINYINT NOT NULL,                 -- 1–5 stars
    review_text TEXT NOT NULL,
    sentiment_score DECIMAL(4,3),                 -- -1.000 (very negative) → 1.000 (very positive)
    sentiment_label ENUM('positive', 'neutral', 'negative'),
    embedding   VECTOR(384),                      -- for semantic search on review content
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (product_id)  REFERENCES products(product_id)
);

ALTER TABLE reviews SET TIFLASH REPLICA 1;

ALTER TABLE reviews DROP INDEX IF EXISTS idx_review_embedding;
ALTER TABLE reviews ADD VECTOR INDEX idx_review_embedding ((VEC_L2_DISTANCE(embedding)));

-- ==========================================
-- 6. SPORTS BETTING (HTAP — alternate vertical)
-- ==========================================

/*
   Betting events and individual wagers.
   Demonstrates the same HTAP pattern as fraud detection applied to sportsbook
   risk management: bets write to TiKV transactionally, liability concentration
   and velocity anomaly queries aggregate live via TiFlash — no ETL.
*/

-- Betting Events: Fixtures available for wagering
CREATE TABLE IF NOT EXISTS betting_events (
    event_id    INT AUTO_INCREMENT PRIMARY KEY,
    tenant_id   VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha', -- tenant isolation
    sport       VARCHAR(50),                        -- e.g. 'Football', 'Basketball', 'Tennis'
    home_team   VARCHAR(100),
    away_team   VARCHAR(100),
    league      VARCHAR(100),                       -- e.g. 'Premier League', 'NBA'
    event_time  DATETIME,
    status      VARCHAR(20) DEFAULT 'active',       -- active, suspended, settled
    home_odds   DECIMAL(6,3),
    away_odds   DECIMAL(6,3),
    draw_odds   DECIMAL(6,3)                        -- NULL for sports with no draw
);

-- Bets: Individual wagers placed by customers
CREATE TABLE IF NOT EXISTS bets (
    bet_id           INT AUTO_INCREMENT PRIMARY KEY,
    tenant_id        VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha', -- tenant isolation
    customer_id      INT,                           -- references customers (no FK for demo simplicity)
    event_id         INT,
    selection        VARCHAR(20),                   -- 'home', 'away', 'draw'
    stake            DECIMAL(10,2),
    odds             DECIMAL(6,3),
    potential_payout DECIMAL(10,2),                -- stake * odds, computed at insert time
    status           VARCHAR(20) DEFAULT 'accepted', -- accepted, voided, suspended, flagged
    ip_address       VARCHAR(45),
    placed_at        DATETIME DEFAULT NOW()
);

ALTER TABLE betting_events SET TIFLASH REPLICA 1;
ALTER TABLE bets SET TIFLASH REPLICA 1;

-- ==========================================
-- 7. COGNITIVE FOUNDATION — SEMANTIC MEMORY (fraud_memory)
-- ==========================================

/*
   fraud_memory — the semantic memory tier of the cognitive foundation.

   Stores confirmed fraud patterns learned from prior investigations as
   vector-indexed records, scoped globally or per-entity (customer/IP).
   This is the table that turns the system from static RAG into compounding
   semantic memory: every confirmed fraud event becomes a recallable pattern
   for future agent sessions.

   Maintained by the five custodial duties:
     - write control:      only confirmed outcomes persist
     - deduplication:      cosine distance < 0.15 → merge
     - reconciliation:     superseded_by chains the supersede event
     - confidence decay:   unreinforced patterns fade over time
     - compaction:         periodic re-clustering keeps the store lean
*/
CREATE TABLE IF NOT EXISTS fraud_memory (
    pattern_id          INT AUTO_INCREMENT PRIMARY KEY,
    scope               ENUM('global', 'entity') NOT NULL DEFAULT 'global',
    entity_ref          VARCHAR(100) NULL,                -- customer_id or ip_address when scope='entity'
    content             TEXT NOT NULL,                    -- semantically-banded pattern description
    embedding           VECTOR(384),                      -- all-MiniLM-L6-v2 output
    confidence          DECIMAL(4,3) NOT NULL DEFAULT 0.850,
    evidence_count      INT NOT NULL DEFAULT 1,
    superseded_by       INT NULL,                         -- pattern_id that supersedes this row
    last_reinforced_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_scope_entity (scope, entity_ref),
    INDEX idx_superseded (superseded_by)
);

ALTER TABLE fraud_memory SET TIFLASH REPLICA 1;

ALTER TABLE fraud_memory DROP INDEX IF EXISTS idx_fraud_mem_embedding;
ALTER TABLE fraud_memory ADD VECTOR INDEX idx_fraud_mem_embedding ((VEC_COSINE_DISTANCE(embedding)));

-- ==========================================
-- 8. COGNITIVE FOUNDATION — EPISODIC CHECKPOINTS (agent_reasoning)
-- ==========================================

/*
   agent_reasoning — structured episodic memory.

   NOT a conversation transcript. Each row is an outcomes-only checkpoint
   written by the agent at the end of an investigation. Fields are the
   contract for the slim summary call: the summary LLM reads ONE row of
   this table, not the entire loop conversation, producing the final
   investigation report from structured input.

   The contract:
     - observation:    what the agent saw (signals, anomalies)
     - hypothesis:     what the agent thinks is happening
     - evidence_refs:  JSON list of pointers (order_ids, IPs, fraud_memory pattern_ids)
     - confidence:     the agent's calibrated confidence in the resolution
     - resolution:     what was decided/done (flag, clear, escalate)
*/
CREATE TABLE IF NOT EXISTS agent_reasoning (
    reasoning_id    INT AUTO_INCREMENT PRIMARY KEY,
    session_id      VARCHAR(36),
    observation     TEXT,
    hypothesis      TEXT,
    evidence_refs   JSON,
    confidence      DECIMAL(4,3),
    resolution      TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session_created (session_id, created_at),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
);

ALTER TABLE agent_reasoning SET TIFLASH REPLICA 1;

-- ==========================================
-- 9. SCHEMA EXTENSIONS FOR EXPANDED SEED CATALOG (Phase A)
-- ==========================================
--
-- The 16 e-commerce fraud patterns in adapters/fraud/SEED_CATALOG reference
-- signals the original schema does not expose. These additions give every
-- catalog pattern a verification path so the agent loop can actually check
-- the signals it shortcuts on.
--
-- Idempotency: all ALTER statements use IF NOT EXISTS (TiDB 5.0+). All new
-- tables use CREATE TABLE IF NOT EXISTS. Safe to re-run.

-- --- customers: synthetic-identity + behavioural signals ---
ALTER TABLE customers ADD COLUMN IF NOT EXISTS email_domain VARCHAR(100);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS timezone VARCHAR(50);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS shipping_address JSON;

-- --- orders: billing/shipping split, device, browser, behavioural ---
ALTER TABLE orders ADD COLUMN IF NOT EXISTS billing_country VARCHAR(50);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_country VARCHAR(50);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_address TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS device_id VARCHAR(100);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_agent VARCHAR(500);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS checkout_seconds DECIMAL(6,2);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_expedited BOOLEAN DEFAULT FALSE;

-- --- products: digital-goods flag for gift-card laundering pattern ---
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_digital_giftcard BOOLEAN DEFAULT FALSE;

-- --- login_attempts: credential-stuffing precursor detection ---
CREATE TABLE IF NOT EXISTS login_attempts (
    attempt_id    INT AUTO_INCREMENT PRIMARY KEY,
    customer_id   INT,
    ip_address    VARCHAR(45),
    country       VARCHAR(50),
    success       BOOLEAN,
    attempted_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_login_customer_time (customer_id, attempted_at),
    INDEX idx_login_ip_time (ip_address, attempted_at)
);
ALTER TABLE login_attempts SET TIFLASH REPLICA 1;

-- --- refunds: serial-returner + triangulation patterns ---
CREATE TABLE IF NOT EXISTS refunds (
    refund_id      INT AUTO_INCREMENT PRIMARY KEY,
    order_id       INT,
    customer_id    INT,
    reason         VARCHAR(200),
    refund_method  VARCHAR(50),                  -- e.g. 'original', 'gift_card', 'store_credit'
    refunded_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_refund_customer (customer_id),
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
ALTER TABLE refunds SET TIFLASH REPLICA 1;

-- --- chargebacks: friendly-fraud / double-dip pattern ---
CREATE TABLE IF NOT EXISTS chargebacks (
    chargeback_id           INT AUTO_INCREMENT PRIMARY KEY,
    customer_id             INT,
    card_last4              CHAR(4),
    filed_at                DATETIME DEFAULT CURRENT_TIMESTAMP,
    delivery_confirmed_at   DATETIME,
    INDEX idx_chargeback_customer (customer_id),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
ALTER TABLE chargebacks SET TIFLASH REPLICA 1;

-- --- freight_forwarders: shipping-redirect detection lookup ---
CREATE TABLE IF NOT EXISTS freight_forwarders (
    forwarder_id     INT AUTO_INCREMENT PRIMARY KEY,
    address_pattern  VARCHAR(255),               -- substring or postcode match
    country          VARCHAR(50),
    known_since      DATETIME DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE freight_forwarders SET TIFLASH REPLICA 1;

-- ==========================================
-- 10. BETTING ADAPTER SCHEMA EXTENSIONS (Phase A)
-- ==========================================

-- --- customer_bonuses: bonus-abuse detection ---
CREATE TABLE IF NOT EXISTS customer_bonuses (
    bonus_id            INT AUTO_INCREMENT PRIMARY KEY,
    customer_id         INT,
    bonus_type          VARCHAR(50),             -- e.g. 'first_deposit', 'reload', 'free_bet'
    claimed_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    qualifying_session  VARCHAR(100),            -- shared session/IP/device fingerprint
    INDEX idx_bonus_customer (customer_id),
    INDEX idx_bonus_session (qualifying_session)
);
ALTER TABLE customer_bonuses SET TIFLASH REPLICA 1;

-- --- odds_history: sharp-money / insider-knowledge detection ---
CREATE TABLE IF NOT EXISTS odds_history (
    history_id    INT AUTO_INCREMENT PRIMARY KEY,
    event_id      INT,
    recorded_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    home_odds     DECIMAL(6,3),
    away_odds     DECIMAL(6,3),
    draw_odds     DECIMAL(6,3),
    suspended     BOOLEAN DEFAULT FALSE,
    INDEX idx_odds_event_time (event_id, recorded_at)
);
ALTER TABLE odds_history SET TIFLASH REPLICA 1;

-- ==========================================
-- 11. TENANT ISOLATION (fact-layer parity)
-- ==========================================
--
-- Every DOMAIN table carries a tenant_id, mirroring the governed fact layer's
-- per-tenant isolation so this repo's own transactional/analytical reads are
-- tenant-scoped too. The 7 primary tables (customers, products, orders,
-- sales_knowledge, reviews, betting_events, bets) declare tenant_id inline
-- above; the ALTERs below (a) add it to the auxiliary domain tables and
-- (b) serve as an idempotent migration for existing clusters where the inline
-- column was added after first deploy. All use IF NOT EXISTS (TiDB 5.0+),
-- safe to re-run. DEFAULT 'demo-bank-alpha' backfills pre-tenant rows onto the
-- primary demo tenant; seed scripts write both demo-bank-alpha and
-- demo-bank-beta explicitly.
--
-- Memory tables (agent_sessions, chat_history, agent_reasoning) are NOT
-- tenant-scoped here: conversational/episodic state is out of the fact layer's
-- scope (that tier is AgentCore Memory's job), and confirmed verdicts now live
-- in the governed fact layer, which enforces tenant isolation via Cedar.

-- Primary domain tables (idempotent on clusters predating the inline column):
ALTER TABLE customers        ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE products         ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE orders           ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE sales_knowledge  ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE reviews          ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE betting_events   ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE bets             ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';

-- Auxiliary domain tables (referenced by fraud/betting adapters, seeds, and the
-- new domain read Lambdas) — same tenant scope:
ALTER TABLE login_attempts     ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE refunds            ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE chargebacks        ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE freight_forwarders ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE customer_bonuses   ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';
ALTER TABLE odds_history       ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'demo-bank-alpha';

-- Tenant indexes for the aux/betting tables that lacked one inline.
ALTER TABLE bets               ADD INDEX IF NOT EXISTS idx_bets_tenant_ip (tenant_id, ip_address);
ALTER TABLE betting_events     ADD INDEX IF NOT EXISTS idx_betting_events_tenant (tenant_id);
ALTER TABLE reviews            ADD INDEX IF NOT EXISTS idx_reviews_tenant (tenant_id);
ALTER TABLE login_attempts     ADD INDEX IF NOT EXISTS idx_login_tenant (tenant_id);
ALTER TABLE refunds            ADD INDEX IF NOT EXISTS idx_refunds_tenant (tenant_id);
ALTER TABLE chargebacks        ADD INDEX IF NOT EXISTS idx_chargebacks_tenant (tenant_id);
ALTER TABLE freight_forwarders ADD INDEX IF NOT EXISTS idx_forwarders_tenant (tenant_id);
ALTER TABLE customer_bonuses   ADD INDEX IF NOT EXISTS idx_bonuses_tenant (tenant_id);
ALTER TABLE odds_history       ADD INDEX IF NOT EXISTS idx_odds_tenant (tenant_id);


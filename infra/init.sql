-- ============================================================
-- order-service 数据库初始化脚本
-- 由 PostgreSQL Docker 镜像自动执行（/docker-entrypoint-initdb.d/）
-- ============================================================

-- 商品表
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    stock INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 订单表（故意不加额外索引，让 COUNT(*) 全表扫描）
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    product_id INT NOT NULL REFERENCES products(id),
    quantity INT NOT NULL,
    total_price DECIMAL(10, 2) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW()
);

-- 10 条商品数据
INSERT INTO products (name, price, stock) VALUES
('笔记本电脑', 5999.00, 100),
('手机', 3999.00, 200),
('耳机', 299.00, 500),
('键盘', 199.00, 300),
('鼠标', 99.00, 800),
('显示器', 1599.00, 150),
('路由器', 399.00, 400),
('摄像头', 249.00, 250),
('移动硬盘', 459.00, 600),
('充电器', 79.00, 1000);

-- 10 万条订单数据（让全表扫描有足够数据量）
DO $$
BEGIN
    FOR i IN 1..100000 LOOP
        INSERT INTO orders (product_id, quantity, total_price, status)
        VALUES (
            (i % 10) + 1,
            (i % 5) + 1,
            ((i % 5) + 1) * 100.00,
            'completed'
        );
    END LOOP;
END $$;

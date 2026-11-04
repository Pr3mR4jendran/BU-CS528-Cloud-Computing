CREATE DATABASE IF NOT EXISTS hw6 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS hw6.countries (
    country VARCHAR(128) PRIMARY KEY,
    is_banned BOOLEAN NOT NULL CHECK (is_banned IN (0, 1))
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS hw6.client_ips (
    client_ip VARCHAR(45) PRIMARY KEY,
    country VARCHAR(128),
    FOREIGN KEY (country) REFERENCES hw6.countries(country)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS hw6.requests (
    request_id VARCHAR(36) PRIMARY KEY,
    request_time DATETIME(6) NOT NULL,
    client_ip VARCHAR(45),
    ip_country_observed BOOLEAN NOT NULL CHECK (ip_country_observed IN (0, 1)),
    gender VARCHAR(16),
    age VARCHAR(16),
    income VARCHAR(16),
    client_time DATETIME,
    requested_file TEXT NOT NULL,
    status_code SMALLINT NOT NULL CHECK (status_code BETWEEN 100 AND 599),
    peer_ip VARCHAR(45),
    method VARCHAR(16) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    traffic_category VARCHAR(16) NOT NULL,
    CHECK (client_ip IS NOT NULL OR ip_country_observed = 0),
    FOREIGN KEY (client_ip) REFERENCES hw6.client_ips(client_ip),
    INDEX ix_requests_run_traffic (run_id, traffic_category)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS hw6.request_country_observations (
    request_id VARCHAR(36) PRIMARY KEY,
    country VARCHAR(128) NOT NULL,
    FOREIGN KEY (request_id) REFERENCES hw6.requests(request_id),
    FOREIGN KEY (country) REFERENCES hw6.countries(country)
) ENGINE=InnoDB;

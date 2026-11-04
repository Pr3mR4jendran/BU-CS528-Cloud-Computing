SELECT r.request_id, r.client_ip, r.age, r.gender, r.client_time, r.income,
       COALESCE(o.country, CASE WHEN r.ip_country_observed THEN ip.country END) AS country
FROM hw6.requests AS r
LEFT JOIN hw6.client_ips AS ip USING (client_ip)
LEFT JOIN hw6.request_country_observations AS o USING (request_id)
WHERE r.run_id = %(run_id)s AND r.traffic_category = 'workload';

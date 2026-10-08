-- A tour of Tessera. Run it with:  python -m tessera -f examples/demo.sql
CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE employees (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, dept_id INTEGER,
  salary REAL, hired TEXT DEFAULT 'unknown'
);
INSERT INTO departments (name) VALUES ('Engineering'), ('Sales'), ('Research'), ('Legal');
INSERT INTO employees (name, dept_id, salary, hired) VALUES
  ('Ada',    1, 142000, '2019-03-01'), ('Linus',  1, 128000, '2020-07-15'),
  ('Grace',  1, 155000, '2018-01-20'), ('Don',    2,  91000, '2021-02-11'),
  ('Peggy',  2,  87500, '2022-09-30'), ('Alan',   3, 133000, '2017-05-05'),
  ('Edsger', 3, 126500, '2016-11-23'), ('Nobody', NULL, 50000, DEFAULT);

SELECT d.name AS department, COUNT(e.id) AS people, ROUND(AVG(e.salary)) AS avg_salary
FROM departments d LEFT JOIN employees e ON e.dept_id = d.id
GROUP BY d.name ORDER BY people DESC, d.name;

SELECT name, salary,
       CASE WHEN salary >= 140000 THEN 'senior' WHEN salary >= 100000 THEN 'mid' ELSE 'junior' END AS band
FROM employees WHERE dept_id IS NOT NULL ORDER BY salary DESC LIMIT 5;

-- indexes change the plan, not the answer
EXPLAIN SELECT * FROM employees WHERE dept_id = 2;
CREATE INDEX emp_dept ON employees (dept_id);
EXPLAIN SELECT * FROM employees WHERE dept_id = 2;
EXPLAIN SELECT e.name, d.name FROM employees e JOIN departments d ON d.id = e.dept_id;

-- transactions are all-or-nothing
BEGIN;
UPDATE employees SET salary = salary * 1.10 WHERE dept_id = 2;
DELETE FROM employees WHERE dept_id IS NULL;
SELECT COUNT(*) AS rows_inside_txn FROM employees;
ROLLBACK;
SELECT COUNT(*) AS rows_after_rollback, SUM(salary) AS payroll FROM employees;

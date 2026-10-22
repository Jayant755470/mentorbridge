CREATE DATABASE IF NOT EXISTS mentorbridge CHARACTER SET utf8mb4;
USE mentorbridge;

CREATE TABLE users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(150) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  role ENUM('mentee','mentor') NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE mentor_profiles (
  user_id INT PRIMARY KEY,
  headline VARCHAR(200) NOT NULL DEFAULT '',
  industry VARCHAR(80) NOT NULL DEFAULT '',
  skills VARCHAR(255) NOT NULL DEFAULT '',
  bio TEXT,
  years_exp INT DEFAULT 0,
  calendly_url VARCHAR(255) DEFAULT NULL,
  slot_minutes INT NOT NULL DEFAULT 30,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- weekly recurring availability (weekday 0 = Monday), times in IST
CREATE TABLE availability (
  id INT AUTO_INCREMENT PRIMARY KEY,
  mentor_id INT NOT NULL,
  weekday TINYINT NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  FOREIGN KEY (mentor_id) REFERENCES users(id) ON DELETE CASCADE
);

-- slot_key is unique while a booking is confirmed and NULL once cancelled,
-- so the database itself prevents two people booking the same mentor slot.
CREATE TABLE bookings (
  id INT AUTO_INCREMENT PRIMARY KEY,
  mentor_id INT NOT NULL,
  mentee_id INT NOT NULL,
  start_at DATETIME NOT NULL,
  end_at DATETIME NOT NULL,
  topic VARCHAR(255) NOT NULL DEFAULT '',
  status ENUM('confirmed','cancelled') NOT NULL DEFAULT 'confirmed',
  room_token CHAR(32) NOT NULL UNIQUE,
  slot_key VARCHAR(40) UNIQUE,
  notes TEXT,
  FOREIGN KEY (mentor_id) REFERENCES users(id),
  FOREIGN KEY (mentee_id) REFERENCES users(id)
);

CREATE TABLE messages (
  id INT AUTO_INCREMENT PRIMARY KEY,
  booking_id INT NOT NULL,
  sender_id INT NOT NULL,
  body TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE
);

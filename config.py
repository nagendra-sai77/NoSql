import os
from dotenv import load_dotenv

# Load .env file from the project root
load_dotenv()

# MongoDB connection URI
MONGO_URI = os.getenv("MONGO_URI", os.getenv("MONGODB_URI", "mongodb://localhost:27017/placement"))
MONGODB_URI = MONGO_URI  # backward-compat alias
DATABASE_NAME = os.getenv("DATABASE_NAME", "placement_db")

# Neo4j connection parameters
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4j123")

# Backward-compat aliases used by app.py
NEO4J_USER = NEO4J_USERNAME
NEO4J_PASS = NEO4J_PASSWORD

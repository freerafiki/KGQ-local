from neo4j import GraphDatabase
from typing import Optional, List, Dict, Any

class GraphHelper:
    """
    Helper class for common Neo4j operations.
    """
    
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
    
    def close(self):
        self.driver.close()

    """ 
    Used to get neighbours of a node. 
    Handles:
    - label filter (collect only neighbors with certain label)
    - direction (in or outgoing neighbours)
    """
    def neighbours(self, node_id: int, label_filter: str | None = None, \
                   direction: str = 'any'):

        if direction == "out":
            pattern = "-[r]->"
        elif direction == "in":
            pattern = "<-[r]-"
        else:  # undirected
            pattern = "-[r]-"

        if label_filter:
            query = f"""
                MATCH (n){pattern}(neighbour:{label_filter})
                WHERE elementId(n) = $node_id
                RETURN neighbour, r
            """

            records, summary, keys = self.driver.execute_query(query, node_id=node_id)
            return [record.data() for record in records]
        # else:
        query = f"""
            MATCH (n){pattern}(neighbour)
            WHERE elementId(n) = $node_id
            RETURN neighbour, r
        """

        records, summary, keys = self.driver.execute_query(query, node_id=node_id)
        return records, summary, keys 
    
    """
    Get the connected neighbours via a particular relation
    """
    def connected_via(self, node_id: int, rel_type: str):
        query = f"""
        MATCH (n)-[:{rel_type}]-(neighbour)
        WHERE elementId(n) = $node_id
        RETURN neighbour
        """
        records, summary, keys = self.driver.execute_query(query, node_id=node_id, rel_type=rel_type)
        return records, summary, keys



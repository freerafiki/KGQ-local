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

    def neighbours(self, node_id: int, label_filter: str | None = None):

        if label_filter:
            query = f"""
                MATCH (n)-[r]-(neighbour:{label_filter})
                WHERE elementId(n) = $node_id
                RETURN neighbour, r
            """

            records, summary, keys = self.driver.execute_query(query, node_id=node_id)
            return [record.data() for record in records]
        # else:
        query = """
            MATCH (n)-[r]-(neighbour)
            WHERE elementId(n) = $node_id
            RETURN neighbour, r
        """

        records, summary, keys = self.driver.execute_query(query, node_id=node_id)
        return [record.data() for record in records]
        
    # ===== BASIC NEIGHBOR OPERATIONS =====
    def get_neighbors(self, node_id: int, rel_type: Optional[str] = None, 
                      direction: str = "any") -> List[Dict[str, Any]]:
        """Get immediate neighbors of a node."""
        
        if direction == "out":
            pattern = f"-[:{rel_type}|*]->" if rel_type else "-[]->"
        elif direction == "in":
            pattern = f"<-[:{rel_type}|*]-" if rel_type else "<-[]-"
        else:  # undirected
            pattern = f"-[:{rel_type}|*]-" if rel_type else "-[]-"
        
        query = f"""
            MATCH (n)-[r]-(neighbour)
            WHERE elementId(n) = $node_id
            RETURN neighbour, r
        """

        records, summary, keys = self.driver.execute_query(query, node_id=node_id)
        return [record.data() for record in records]
            
    # not really sure if this helps    
    # def get_neighbor_ids(self, node_id: int, rel_type: Optional[str] = None) -> List[int]:
    #     """Get just the IDs of neighboring nodes (lightweight)."""
        
    #     pattern = f"-[:{rel_type}]->" if rel_type else "-[]->"
        
    #     query = f"""
    #     MATCH (n {{id: $node_id}}){pattern}(m)
    #     RETURN id(m) AS neighbor_id
    #     """
    #     records, summary, keys = self.driver.execute_query(query, node_id=node_id)
    #     return [record["neighbor_id"] for record in records]
    

    # ===== MULTI-HOP TRAVERSAL =====
    def get_n_hop_neighbors(self, node_id: int, max_hops: int, 
                            rel_types: List[str] = None) -> List[Dict[str, Any]]:
        """Get nodes reachable within N hops."""
        
        rel_filter = "|".join(rel_types) if rel_types else "*"
        pattern = f"-[:{rel_filter}*1..{max_hops}]-"
        
        query = f"""
        MATCH (n {{id: $node_id}}){pattern}(neighbor)
        RETURN 
            neighbor, 
            length(shortestPath((n)-[*..{max_hops}-(neighbor)]) - 1) AS hops
        ORDER BY hops
        """

        records, summary, keys = self.driver.execute_query(query, node_id=node_id)
        return [record.data() for record in records]
        
    def connected_via(self, node_id: int, rel_type: str):
        query = f"""
        MATCH (n)-[:{rel_type}]-(neighbour)
        WHERE elementId(n) = $node_id
        RETURN neighbour
        """
        records, summary, keys = self.driver.execute_query(query, node_id=node_id, rel_type=rel_type)
        return [record.data() for record in records]

    # ===== ONE RELATIONSHIP FOLLOW =====
    def follow_relationship(self, node_id: int, rel_type: str) -> List[Dict[str, Any]]:
        """Follow a specific relationship type from a node."""
        
        query = f"""
        MATCH (n {{id: $node_id}})-[r:{rel_type}]->(target)
        RETURN 
            target, 
            properties(r) AS rel_props,
            type(r) AS rel_type
        """
        
        records, summary, keys = self.driver.execute_query(query, node_id=node_id)
        return [record.data() for record in records]
    

    # # ===== BATCH NEIGHBOR LOOKUP =====
    # def get_multiple_node_neighbors(self, node_ids: List[int], 
    #                                 rel_type: Optional[str] = None) -> Dict[int, List[Any]]:
    #     """Get neighbors for multiple nodes in one query."""
        
    #     pattern = f"-[:{rel_type}]->" if rel_type else "-[]->"
        
    #     query = f"""
    #     MATCH (n) WHERE n.id IN $node_ids{pattern}(neighbor)
    #     RETURN n.id AS source_id, neighbor
    #     ORDER BY source_id
    #     """

    #     records, summary, keys = self.driver.execute_query(query, node_ids=node_ids)
    #     return [record.data() for record in records]

    #     grouped = {}
    #     for record in records:
    #         source_id = record["source_id"]
    #         neighbor = record["neighbor"]
    #         if source_id not in grouped:
    #             grouped[source_id] = []
    #         grouped[source_id].append(dict(neighbor))
        
    #     return grouped


import time
import requests
import openai
import pinecone 
from src.api.job_status import JobStatus
from src.api.batch_status import BatchStatus

# TODO: load this from an ENV Var or config file
base_request_url = 'http://localhost:5000' 


def process_batch(batch):
    embeddings_type = batch['embeddings_metadata']['embeddings_type']
    if embeddings_type == 'OPEN_AI':
        try:
            vectors_uploaded = embed_openai_batch(batch)
            update_job_status(batch['job_id'], BatchStatus.COMPLETED, batch['batch_id']) if vectors_uploaded else update_job_status(batch['job_id'], BatchStatus.FAILED, batch['batch_id'])
        except Exception as e:
            print('Error embedding batch:', e)
            update_job_status(batch['job_id'], BatchStatus.FAILED, batch['batch_id'])
    else:
        print('Unsupported embeddings type:', embeddings_type)
        update_job_status(batch['job_id'], BatchStatus.FAILED, batch['batch_id'])

def embed_openai_batch(batch):
    openai.api_key = batch['embeddings_metadata']['api_key']
    chunked_data = chunk_data(batch['source_data'], batch['embeddings_metadata']['chunk_size'], batch['embeddings_metadata']['chunk_overlap'])

    # TODO: Add concurrency to this
    for i in range(5):
        try:
            response = openai.Embedding.create(
                model= "text-embedding-ada-002",
                input=chunked_data
            )
            
            if response["data"][0]["embedding"]:
                break
        except Exception as e:
            print('Open AI Embedding API call failed:', e)
            time.sleep(2**i)  # Exponential backoff: 1, 2, 4, 8, 16 seconds.

            if i == 4:
                print("Open AI Embedding API call failed after 5 attempts. Adding batch to retry queue.")
                update_job_status(batch['job_id'], BatchStatus.Failed, batch['batch_id'])
                return
        
        if i == 4:
                print("Open AI Embedding API call did not return a value for the embeddings after 5 attempts. Adding batch to retry queue.")
                update_job_status(batch['job_id'], BatchStatus.Failed, batch['batch_id'])
                return

    embeddings = response["data"][0]["embedding"]
    return write_embeddings_to_vector_db(embeddings, chunked_data, batch['vector_db_metadata'], batch['batch_id'], batch['job_id'])   

def chunk_data(data, chunk_size, chunk_overlap):
    chunks = []
    for i in range(0, len(data), chunk_size - chunk_overlap):
        chunks.append(data[i:i + chunk_size])
    return chunks

def write_embeddings_to_vector_db(embeddings, chunked_data, vector_db_metadata, batch_id, job_id):
    if vector_db_metadata['vector_db_type'] == 'PINECONE':
        upsert_list = create_source_chunk_dict(chunked_data, embeddings, batch_id, job_id)
        return write_embeddings_to_pinecone(upsert_list, vector_db_metadata)
    else:
        print('Unsupported vector DB type:', vector_db_metadata['vector_db_type'])

def create_source_chunk_dict(chunked_data, embeddings, batch_id, job_id):
    embedding_source_pair = zip(embeddings, chunked_data)
    upsert_list = []
    for i, (embedding, source_text) in enumerate(embedding_source_pair):
        upsert_list.append(
            {"id":f"{job_id}_{batch_id}_{i}", 
            "values": embedding, 
            "metadata": {"source_text": source_text}})
    return upsert_list

def write_embeddings_to_pinecone(upsert_list, vector_db_metadata):
    pinecone.init(api_key=vector_db_metadata['api_key'], environment=vector_db_metadata['environment'])
    index = pinecone.Index(vector_db_metadata['index_name'])
    if not index:
        print(f"Index {vector_db_metadata['index_name']} does not exist in environment {vector_db_metadata['environment']}")
        return None
    
    #TODO: parallelize upsert
    return index.upsert(vectors=upsert_list)
    
# this implementation mocks the data service. Using this instead because DB not implement yet
def update_job_status(job_id, batch_status, batch_id):
    headers = {"Content-Type": "application/json"}
    data = {
        "batch_id": batch_id,
        "batch_status": batch_status,
    }
    response = requests.put(f'{base_request_url}/jobs/{job_id}', headers=headers, data=data)

    # TODO: implement webhook callback logic - can pass it back in the response and call it with the response code

if __name__ == "__main__":
    while True:
        response = requests.get(f'{base_request_url}/dequeue')
        if response.status_code == 404:
            time.sleep(5)
        elif response.status_code == 200:
            batch = response.json['batch']
            process_batch(batch)
        else:
            print('Unexpected status code:', response.status_code)


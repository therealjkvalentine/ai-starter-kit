import os
import sys
sys.path.append("../")
from dotenv import load_dotenv
load_dotenv("../export.env")

from utils.sambanova_endpoint import SambaNovaEndpoint
from langchain.prompts import load_prompt
from langchain_core.output_parsers import StrOutputParser
from langchain.chains import ReduceDocumentsChain, LLMChain
from langchain.chains.combine_documents.stuff import StuffDocumentsChain
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.output_parsers import CommaSeparatedListOutputParser, StructuredOutputParser, ResponseSchema
from langchain.chains.retrieval import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from vectordb.vector_db import VectorDb
from langchain.schema import Document
import concurrent.futures

model = SambaNovaEndpoint(
            model_kwargs={
                "do_sample": True, 
                "temperature": 0.01,
                "max_tokens_to_generate": 1500,
            }
        ) 

def load_conversation(transcription, transcription_path):
    doc = Document(page_content=transcription, metadata={"source": transcription_path})
    return [doc]

def reduce_call(conversation):
    reduce_prompt = load_prompt("./prompts/reduce.yaml")
    reduce_chain = LLMChain(llm=model, prompt=reduce_prompt)  
    combine_documents_chain = StuffDocumentsChain(
        llm_chain=reduce_chain, document_variable_name="transcription_chunks"
    )
    # Combines and iteravely reduces the documents
    reduce_documents_chain = ReduceDocumentsChain(
        # This is final chain that is called.
        combine_documents_chain=combine_documents_chain,
        # If documents exceed context for `StuffDocumentsChain`
        collapse_documents_chain=combine_documents_chain,
        # The maximum number of tokens to group documents into.
        token_max=1200,  
    )
    print("reducing call")
    new_document = reduce_documents_chain.invoke(conversation)["output_text"]
    print("call reduced")
    return new_document

def get_summary(conversation, model=model):
    summarization_prompt=load_prompt("./prompts/summarization.yaml")
    output_parser = StrOutputParser()
    summarization_chain = summarization_prompt | model | output_parser
    input_variables={"conversation": conversation}
    print("summarizing")
    summarization_response = summarization_chain.invoke(input_variables)
    print("summarizing done")
    return summarization_response

def classify_main_topic(conversation, classes, model=model):
    topic_classification_prompt=load_prompt("./prompts/topic_classifications.yaml")
    list_output_parser = CommaSeparatedListOutputParser()
    list_format_instructions = list_output_parser.get_format_instructions()
    topic_classifcation_chain = topic_classification_prompt | model | list_output_parser
    input_variables={"conversation":conversation, "topic_classes" : "\n\t- ".join(classes), "format_instructions": list_format_instructions}
    print("cassification")
    topic_classifcation_response = topic_classifcation_chain.invoke(input_variables)
    print("classification done")
    return topic_classifcation_response
    
def get_entities(conversation, entities, model=model):
    ner_prompt = load_prompt("./prompts/ner.yaml")
    response_schemas = []
    for entity in entities:
        response_schemas.append(ResponseSchema(name=entity, description=f"{entity}s find in conversation", type="list"))
    entities_output_parser = StructuredOutputParser.from_response_schemas(response_schemas)
    ner_chain = ner_prompt | model | entities_output_parser
    input_variables={"conversation":conversation,
                     "entities" : "\n\t- ".join(entities), 
                     "format_instructions":entities_output_parser.get_format_instructions()
                    }
    print("extracting entities")
    ner_response = ner_chain.invoke(input_variables)
    print("extracting entities done")
    return ner_response

def get_sentiment(conversation, model=model):
    sentiment_analysis_prompt = load_prompt("./prompts/sentiment_analysis2.yaml")
    output_parser = StrOutputParser()
    sentiment_analysis_chain = sentiment_analysis_prompt | model | output_parser
    input_variables={"conversation":conversation}
    print("sentiment analysis")
    sentiment_analysis_response = sentiment_analysis_chain.invoke(input_variables)
    print("sentiment analysis done")
    return sentiment_analysis_response

def set_retriever(documents_path):
    print("setting retriever")
    vdb=VectorDb()
    retriever = vdb.create_vdb(documents_path,1000,200,"faiss",None).as_retriever()
    print("retriever")
    return retriever

def factual_accuracy_analysis(conversation, retriever, model=model):
    factual_accuracy_analysis_response_schemas = [ResponseSchema(name="correct",
                                                                 description="wether or not the provided information is correct",
                                                                 type="bool"
                                                                 ),
                                                  ResponseSchema(name="errors",
                                                                 description="list of summarized errors made by the agent, if there is no errors, emplty list" ,
                                                                 type="list")
                                                ]
    factual_accuracy_analysis_output_parser = StructuredOutputParser.from_response_schemas(factual_accuracy_analysis_response_schemas)
    format_instructions=factual_accuracy_analysis_output_parser.get_format_instructions()
    retrieval_qa_chat_prompt = load_prompt("./prompts/factual_accuracy_analysis.yaml")
    combine_docs_chain = create_stuff_documents_chain(
        model, retrieval_qa_chat_prompt
    )
    retrieval_chain = create_retrieval_chain(retriever, combine_docs_chain)
    input_variables={"input":conversation,
                     "format_instructions":format_instructions
                    }
    model_response=retrieval_chain.invoke(input_variables)["answer"]
    print("factual check")
    factual_accuracy_analysis_response=factual_accuracy_analysis_output_parser.invoke(model_response)
    print("factual check done")
    return factual_accuracy_analysis_response

def get_chunks(documents):
    #split long document
    splitter = RecursiveCharacterTextSplitter(chunk_size= 800, chunk_overlap= 200)
    return  splitter.split_documents(documents)

def call_analysis_parallel(conversation, documents_path, classes_list, entities_list):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Submitting tasks to executor
        reduced_conversation_future = executor.submit(reduce_call, conversation=conversation)
        retriever = set_retriever(documents_path=documents_path)
        reduced_conversation = reduced_conversation_future.result()
        summary_future = executor.submit(get_summary, conversation=reduced_conversation)
        classification_future = executor.submit(classify_main_topic, conversation=reduced_conversation, classes=classes_list)
        entities_future = executor.submit(get_entities, conversation=reduced_conversation, entities=entities_list)
        sentiment_future = executor.submit(get_sentiment, conversation=reduced_conversation)
        factual_analysis_future = executor.submit(factual_accuracy_analysis, conversation=reduced_conversation, retriever = retriever)

        # Retrieving results
        summary = summary_future.result()
        classification = classification_future.result()
        entities = entities_future.result()
        sentiment = sentiment_future.result()
        factual_analysis = factual_analysis_future.result()

    quality_score = 58  # TO-DO create method

    return {
        "summary": summary,
        "classification": classification,
        "entities": entities,
        "sentiment": sentiment,
        "factual_analysis": factual_analysis,
        "quality_score": quality_score
    }

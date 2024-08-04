# Databricks notebook source
# MAGIC %md 
# MAGIC # init notebook setting up the backend. 
# MAGIC
# MAGIC Do not edit the notebook, it contains import and helpers for the demo
# MAGIC
# MAGIC <!-- Collect usage data (view). Remove it to disable collection or disable tracker during installation. View README for more details.  -->
# MAGIC <img width="1px" src="https://ppxrzfxige.execute-api.us-west-2.amazonaws.com/v1/analytics?category=data-science&org_id=2638284274323173&notebook=%2F_resources%2F00-init&demo_name=llm-rag-chatbot&event=VIEW&path=%2F_dbdemos%2Fdata-science%2Fllm-rag-chatbot%2F_resources%2F00-init&version=1">

# COMMAND ----------

# MAGIC %run ../config

# COMMAND ----------


dbutils.widgets.text("reset_all_data", "false", "Reset Data")
reset_all_data = dbutils.widgets.get("reset_all_data") == "true"

# COMMAND ----------

from IPython.core.magic import register_cell_magic

# When running in a job, writting to the local file fails. This simply skip it as we ship the file in the repo so there is no need to write it if it's running from the repo
# Note: we don't ship the chain file when we install it with dbdemos.instal(...).
@register_cell_magic
def writefile(line, cell):
    filename = line.strip()
    try:
      folder_path = os.path.dirname(filename)
      if len(folder_path) > 0:
        os.makedirs(folder_path, exist_ok=True)
      with open(filename, 'w') as f:
          f.write(cell)
          print('file overwritten')
    except Exception as e:
      print(f"WARN: could not write the file {filename}. If it's running as a job it's to be expected, otherwise something is off - please print the message for more details: {e}")

# COMMAND ----------

from pyspark.sql.functions import pandas_udf
import pandas as pd
import pyspark.sql.functions as F
from pyspark.sql.functions import col, udf, length, pandas_udf
import os
import mlflow
import yaml
from typing import Iterator
from mlflow import MlflowClient
mlflow.set_registry_uri('databricks-uc')

# Workaround for a bug fix that is in progress
mlflow.spark.autolog(disable=True)

import warnings
warnings.filterwarnings("ignore")
# Disable MLflow warnings
import logging
logging.getLogger('mlflow').setLevel(logging.ERROR)

# COMMAND ----------

if reset_all_data:
  print(f'clearing up db {dbName}')
  spark.sql(f"DROP DATABASE IF EXISTS `{dbName}` CASCADE")

# COMMAND ----------

def use_and_create_db(catalog, dbName, cloud_storage_path = None):
  print(f"USE CATALOG `{catalog}`")
  spark.sql(f"USE CATALOG `{catalog}`")
  spark.sql(f"""create database if not exists `{dbName}` """)

assert catalog not in ['hive_metastore', 'spark_catalog']
#If the catalog is defined, we force it to the given value and throw exception if not.
if len(catalog) > 0:
  current_catalog = spark.sql("select current_catalog()").collect()[0]['current_catalog()']
  if current_catalog != catalog:
    catalogs = [r['catalog'] for r in spark.sql("SHOW CATALOGS").collect()]
    if catalog not in catalogs:
      spark.sql(f"CREATE CATALOG IF NOT EXISTS {catalog}")
      if catalog == 'dbdemos':
        spark.sql(f"ALTER CATALOG {catalog} OWNER TO `account users`")
  use_and_create_db(catalog, dbName)

if catalog == 'dbdemos':
  try:
    spark.sql(f"GRANT CREATE, USAGE on DATABASE {catalog}.{dbName} TO `account users`")
    spark.sql(f"ALTER SCHEMA {catalog}.{dbName} OWNER TO `account users`")
  except Exception as e:
    print("Couldn't grant access to the schema to all users:"+str(e))    

print(f"using catalog.database `{catalog}`.`{dbName}`")
spark.sql(f"""USE `{catalog}`.`{dbName}`""")    

# COMMAND ----------

if not spark.catalog.tableExists("databricks_documentation") or spark.table("databricks_documentation").isEmpty() or \
    not spark.catalog.tableExists("eval_set_databricks_documentation") or spark.table("eval_set_databricks_documentation").isEmpty():
  spark.sql('''CREATE TABLE IF NOT EXISTS databricks_documentation (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY,
            url STRING,
            content STRING
          ) TBLPROPERTIES (delta.enableChangeDataFeed = true)''')
  (spark.createDataFrame(pd.read_parquet('https://notebooks.databricks.com/demos/dbdemos-dataset/llm/databricks-documentation/databricks_documentation.parquet'))
   .drop('title').write.mode('overwrite').saveAsTable("databricks_documentation"))
  (spark.createDataFrame(pd.read_parquet('https://notebooks.databricks.com/demos/dbdemos-dataset/llm/databricks-documentation/databricks_doc_eval_set.parquet'))
   .write.mode('overwrite').saveAsTable("eval_set_databricks_documentation"))
  #Make sure enableChangeDataFeed is enabled
  spark.sql('ALTER TABLE databricks_documentation SET TBLPROPERTIES (delta.enableChangeDataFeed = true)')


def display_txt_as_html(txt):
    txt = txt.replace('\n', '<br/>')
    displayHTML(f'<div style="max-height: 150px">{txt}</div>')

# COMMAND ----------

# DBTITLE 1,Optional: Allowing Model Serving IPs
#If your workspace has ip access list, you need to allow your model serving endpoint to hit your AI gateway. Based on your region, IPs might change. Please reach out your Databrics Account team for more details.

#def allow_serverless_ip():
#  from databricks.sdk import WorkspaceClient
#  from databricks.sdk.service import settings
#
#  w = WorkspaceClient()
#
#  # cleanup
#  w.ip_access_lists.delete(ip_access_list_id='xxxx')
#  created = w.ip_access_lists.create(label=f'serverless-model-serving',
#                                    ip_addresses=['xxxx/32'],
#                                    list_type=settings.ListType.ALLOW)

# COMMAND ----------

# MAGIC %md 
# MAGIC
# MAGIC ## Helpers to get catalog and index status:

# COMMAND ----------

# Helper function
def get_latest_model_version(model_name):
    mlflow_client = MlflowClient(registry_uri="databricks-uc")
    latest_version = 1
    for mv in mlflow_client.search_model_versions(f"name='{model_name}'"):
        version_int = int(mv.version)
        if version_int > latest_version:
            latest_version = version_int
    return latest_version

# COMMAND ----------

# DBTITLE 1,endpoint
import time

def endpoint_exists(vsc, vs_endpoint_name):
  try:
    return vs_endpoint_name in [e['name'] for e in vsc.list_endpoints().get('endpoints', [])]
  except Exception as e:
    #Temp fix for potential REQUEST_LIMIT_EXCEEDED issue
    if "REQUEST_LIMIT_EXCEEDED" in str(e):
      print("WARN: couldn't get endpoint status due to REQUEST_LIMIT_EXCEEDED error. The demo will consider it exists")
      return True
    else:
      raise e

def wait_for_vs_endpoint_to_be_ready(vsc, vs_endpoint_name):
  for i in range(180):
    try:
      endpoint = vsc.get_endpoint(vs_endpoint_name)
    except Exception as e:
      #Temp fix for potential REQUEST_LIMIT_EXCEEDED issue
      if "REQUEST_LIMIT_EXCEEDED" in str(e):
        print("WARN: couldn't get endpoint status due to REQUEST_LIMIT_EXCEEDED error. Please manually check your endpoint status")
        return
      else:
        raise e
    status = endpoint.get("endpoint_status", endpoint.get("status"))["state"].upper()
    if "ONLINE" in status:
      return endpoint
    elif "PROVISIONING" in status or i <6:
      if i % 20 == 0: 
        print(f"Waiting for endpoint to be ready, this can take a few min... {endpoint}")
      time.sleep(10)
    else:
      raise Exception(f'''Error with the endpoint {vs_endpoint_name}. - this shouldn't happen: {endpoint}.\n Please delete it and re-run the previous cell: vsc.delete_endpoint("{vs_endpoint_name}")''')
  raise Exception(f"Timeout, your endpoint isn't ready yet: {vsc.get_endpoint(vs_endpoint_name)}")

# COMMAND ----------

# DBTITLE 1,index
def index_exists(vsc, endpoint_name, index_full_name):
    try:
        vsc.get_index(endpoint_name, index_full_name).describe()
        return True
    except Exception as e:
        if 'RESOURCE_DOES_NOT_EXIST' not in str(e):
            print(f'Unexpected error describing the index. This could be a permission issue.')
            raise e
    return False
    
def wait_for_index_to_be_ready(vsc, vs_endpoint_name, index_name):
  for i in range(180):
    idx = vsc.get_index(vs_endpoint_name, index_name).describe()
    index_status = idx.get('status', idx.get('index_status', {}))
    status = index_status.get('detailed_state', index_status.get('status', 'UNKNOWN')).upper()
    url = index_status.get('index_url', index_status.get('url', 'UNKNOWN'))
    if "ONLINE" in status:
      return
    if "UNKNOWN" in status:
      print(f"Can't get the status - will assume index is ready {idx} - url: {url}")
      return
    elif "PROVISIONING" in status:
      if i % 40 == 0: print(f"Waiting for index to be ready, this can take a few min... {index_status} - pipeline url:{url}")
      time.sleep(10)
    else:
        raise Exception(f'''Error with the index - this shouldn't happen. DLT pipeline might have been killed.\n Please delete it and re-run the previous cell: vsc.delete_index("{index_name}, {vs_endpoint_name}") \nIndex details: {idx}''')
  raise Exception(f"Timeout, your index isn't ready yet: {vsc.get_index(index_name, vs_endpoint_name)}")

def wait_for_model_serving_endpoint_to_be_ready(ep_name):
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import EndpointStateReady, EndpointStateConfigUpdate
    import time

    # TODO make the endpoint name as a param
    # Wait for it to be ready
    w = WorkspaceClient()
    state = ""
    for i in range(200):
        state = w.serving_endpoints.get(ep_name).state
        if state.config_update == EndpointStateConfigUpdate.IN_PROGRESS:
            if i % 40 == 0:
                print(f"Waiting for endpoint to deploy {ep_name}. Current state: {state}")
            time.sleep(10)
        elif state.ready == EndpointStateReady.READY:
          print('endpoint ready.')
          return
        else:
          break
    raise Exception(f"Couldn't start the endpoint, timeout, please check your endpoint for more details: {state}")

# COMMAND ----------

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pyspark.sql.types import StringType
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
#Add retries with backoff to avoid 429 while fetching the doc
retries = Retry(
    total=3,
    backoff_factor=3,
    status_forcelist=[429],
)

def download_databricks_documentation_articles(max_documents=None):
    # Fetch the XML content from sitemap
    response = requests.get(DATABRICKS_SITEMAP_URL)
    root = ET.fromstring(response.content)

    # Find all 'loc' elements (URLs) in the XML
    urls = [loc.text for loc in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    if max_documents:
        urls = urls[:max_documents]

    # Create DataFrame from URLs
    df_urls = spark.createDataFrame(urls, StringType()).toDF("url").repartition(10)

    # Pandas UDF to fetch HTML content for a batch of URLs
    @pandas_udf("string")
    def fetch_html_udf(urls: pd.Series) -> pd.Series:
        adapter = HTTPAdapter(max_retries=retries)
        http = requests.Session()
        http.mount("http://", adapter)
        http.mount("https://", adapter)
        def fetch_html(url):
            try:
                response = http.get(url)
                if response.status_code == 200:
                    return response.content
            except requests.RequestException:
                return None
            return None

        with ThreadPoolExecutor(max_workers=200) as executor:
            results = list(executor.map(fetch_html, urls))
        return pd.Series(results)

    # Pandas UDF to process HTML content and extract text
    @pandas_udf("string")
    def download_web_page_udf(html_contents: pd.Series) -> pd.Series:
        def extract_text(html_content):
            if html_content:
                soup = BeautifulSoup(html_content, "html.parser")
                article_div = soup.find("div", itemprop="articleBody")
                if article_div:
                    return str(article_div).strip()
            return None

        return html_contents.apply(extract_text)

    # Apply UDFs to DataFrame
    df_with_html = df_urls.withColumn("html_content", fetch_html_udf("url"))
    final_df = df_with_html.withColumn("text", download_web_page_udf("html_content"))

    # Select and filter non-null results
    final_df = final_df.select("url", "text").filter("text IS NOT NULL").cache()
    if final_df.isEmpty():
      raise Exception("Dataframe is empty, couldn't download Databricks documentation, please check sitemap status.")

    return final_df

# COMMAND ----------

def display_gradio_app(space_name = "databricks-demos-chatbot"):
    displayHTML(f'''<div style="margin: auto; width: 1000px"><iframe src="https://{space_name}.hf.space" frameborder="0" width="1000" height="950" style="margin: auto"></iframe></div>''')

# COMMAND ----------

# DBTITLE 1,Cleanup utility to remove demo assets
def cleanup_demo(catalog, db, serving_endpoint_name, vs_index_fullname):
  vsc = VectorSearchClient()
  try:
    vsc.delete_index(endpoint_name = VECTOR_SEARCH_ENDPOINT_NAME, index_name=vs_index_fullname)
  except Exception as e:
    print(f"can't delete index {VECTOR_SEARCH_ENDPOINT_NAME} {vs_index_fullname} - might not be existing: {e}")
  try:
    WorkspaceClient().serving_endpoints.delete(serving_endpoint_name)
  except Exception as e:
    print(f"can't delete serving endpoint {serving_endpoint_name} - might not be existing: {e}")
  spark.sql(f'DROP SCHEMA `{catalog}`.`{db}` CASCADE')

# COMMAND ----------

def pprint(obj):
  import pprint
  pprint.pprint(obj, compact=True, indent=1, width=100)

# COMMAND ----------

#Temp workaround to test if a table exists in shared cluster mode in DBR 14.2 (see SASP-2467)
def table_exists(table_name):
    try:
        spark.table(table_name).isEmpty()
    except:
        return False
    return True

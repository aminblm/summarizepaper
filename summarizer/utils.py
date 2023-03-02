import time
import requests
from .models import ArxivPaper, SummaryPaper, PickledData, AIassistant
from django.contrib.auth.models import User, AnonymousUser
import asyncio
from asgiref.sync import sync_to_async,async_to_sync
from channels.db import database_sync_to_async
import pdfminer
from io import StringIO
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfpage import PDFPage
from django.shortcuts import redirect
from channels.layers import get_channel_layer
import re
from django.http import HttpResponse
import os
from django.conf import settings
import ast
from io import StringIO
from html.parser import HTMLParser
from langchain import OpenAI, PromptTemplate, LLMChain
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.text_splitter import CharacterTextSplitter
from langchain.vectorstores import ElasticVectorSearch, Pinecone, Weaviate, FAISS, Chroma
from langchain.chains.mapreduce import MapReduceChain
from langchain.prompts import PromptTemplate
from langchain.docstore.document import Document
from langchain.chains.summarize import load_summarize_chain
from langchain.chains.question_answering import load_qa_chain
from langchain.chains.qa_with_sources import load_qa_with_sources_chain
from langchain.llms import OpenAI
from langchain.callbacks import get_openai_callback
import pickle
from django.utils.translation import get_language_info
import nltk
from xml.etree import ElementTree
import urllib, urllib.request


channel_layer = get_channel_layer()
model="text-davinci-003"#"text-davinci-002"
temp=0.3
method="fromembeddingsandabstract"#"fromembeddings"#"langchain"#quentin


def openaipricing(model_name):
    #return cost per token in dollars
    if 'davinci' in model_name:
        cost=0.02
    elif 'babbage' in model_name:
        cost=0.0005
    elif 'curie' in model_name:
        cost=0.002
    elif 'ada' in model_name:
        cost=0.0004
    else:
        cost=1.

    return cost/1000.

def dependable_faiss_import():# -> Any:
    """Import faiss if available, otherwise raise error."""
    try:
        import faiss
    except ImportError:
        raise ValueError(
            "Could not import faiss python package. "
            "Please it install it with `pip install faiss` "
            "or `pip install faiss-cpu` (depending on Python version)."
        )
    return faiss

def readpaper(arxiv_id):
    paper=ArxivPaper.objects.prefetch_related('authors').filter(arxiv_id=arxiv_id)[0]

    return paper

def getpaperabstract(arxiv_id):
    paperabstract=ArxivPaper.objects.filter(arxiv_id=arxiv_id).values_list('abstract',flat=True)[0]

    return paperabstract

def getuserinst(user):
    if User.objects.filter(username=user).exists():
        userinst = User.objects.get(username=user)
        # Do something with the admin_user instance
    else:
        userinst = None# AnonymousUser()

    return userinst

def storeconversation(arxiv_id,query,response,user,lang):

    AIassistant.objects.create(
        arxiv_id=arxiv_id,
        query=query,
        response=response,
        user=user,
        lang=lang
    )


def storepickle(arxiv_id,docstore_pickle,index_to_docstore_id_pickle,buffer):

    obj, created = PickledData.objects.update_or_create(
        arxiv_id=arxiv_id,
        defaults={
            'docstore_pickle': docstore_pickle,
            'index_to_docstore_id_pickle': index_to_docstore_id_pickle,
            'buffer':buffer
        }
    )

    return created

def getstorepickle(arxiv_id):

    if PickledData.objects.filter(arxiv_id=arxiv_id).exists():
        print("Data found with arxiv_id =", arxiv_id)
        pickledata=PickledData.objects.filter(arxiv_id=arxiv_id)[0]
    else:
        print("No data found with arxiv_id =", arxiv_id)
        pickledata=''

    return pickledata



async def createindex(arxiv_id,book_text,api_key):

    text_splitter = CharacterTextSplitter.from_tiktoken_encoder(separator = "\n\n", chunk_size=800, chunk_overlap=200)#limit at 8096 tokens
    texts = text_splitter.split_text(book_text)

    '''
    text_splitter = CharacterTextSplitter(
    separator = "\n",
    chunk_size = 1000,
    chunk_overlap  = 200,
    length_function = len,
    )
    texts = text_splitter.split_text(book_text)
    '''
    print('tttettxtxtxtxtxtxtxtttzetet',texts)

    embeddings = OpenAIEmbeddings(openai_api_key=api_key)#text-embedding-ada-002 used in background

    new_docsearch=embeddings

    #docsearch = FAISS.from_texts(texts, new_docsearch,metadatas=[{"source": str(i)} for i in range(len(texts))])

    metadatas = [
        {"source": f"from {texts[i][0:30]} --- to --- {texts[i][len(texts[i])-30:-1]}"}
        for i in range(len(texts))
    ]

    # Print the metadata
    #for metadata in metadatas:
    #    print('met',metadata)

    docsearch = FAISS.from_texts(texts, new_docsearch,metadatas=metadatas)



    #input('jkll')
    #docsearch = Chroma.from_texts(texts, embeddings)
    #tu=FAISS.save_local(docsearch,"savedocsearch")

    print('docsearchhhhhhhhhhhhhhh index',docsearch.index)
    print('docsearchhhhhhhhhhhhhhh doc',docsearch.docstore)
    print('docsearchhhhhhhhhhhhhhh id',docsearch.index_to_docstore_id)
    # save index separately since it is not picklable
    faiss = dependable_faiss_import()
    # serialize the index to a byte buffer
    #buffer = bytearray()
    #faiss.write_index(docsearch.index, buffer)
    chunk = faiss.serialize_index(docsearch.index)
    buffer_pickle = pickle.dumps(chunk)

    # save docstore and index_to_docstore_id
    docstore_pickle = pickle.dumps(docsearch.docstore)
    index_to_docstore_id_pickle = pickle.dumps(docsearch.index_to_docstore_id)

    # update or create a PickledData object with the given arxiv_id
    c = asyncio.create_task(sync_to_async(storepickle)(arxiv_id,docstore_pickle,index_to_docstore_id_pickle,buffer_pickle))
    created = await c

    print('ok created',created)
    return created

async def chatbot(arxiv_id,language,query,api_key,sum=None,user=None):
    print('in chatbot')

    li = get_language_info(language)
    language2 = li['name']
    print('language2',language2)

    c = asyncio.create_task(sync_to_async(getstorepickle)(arxiv_id))

    getstoredpickle = await c

    if getstoredpickle != '':
        # deserialize the index from a byte buffer
        #index_buffer = faiss.read_index(storedpickle.buffer)
        faiss = dependable_faiss_import()

        index_buffer = faiss.deserialize_index(pickle.loads(getstoredpickle.buffer))   # identical to index

        docstore_pickle=pickle.loads(getstoredpickle.docstore_pickle)
        index_to_docstore_id_pickle=pickle.loads(getstoredpickle.index_to_docstore_id_pickle)

        #from langchain.chains import ChatVectorDBChain

        if sum==1:
            llm = OpenAI(temperature=0.3,max_tokens=800,frequency_penalty=0.6, presence_penalty=0.6,openai_api_key=api_key)
        else:
            llm = OpenAI(temperature=0.3,max_tokens=700,openai_api_key=api_key)

        '''
        For streaming
        from langchain.callbacks.base import CallbackManager
        from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler

        llm = OpenAI(streaming=True, callback_manager=CallbackManager([StreamingStdOutCallbackHandler()]), verbose=True, temperature=0.3,openai_api_key=api_key)
        #llm = OpenAI(model_name=modelforsummarizing,max_tokens=1000,best_of=1,n=1,temperature=0.3,openai_api_key=api_key)
        #best_of=2,streaming=True
        '''
        embeddings = OpenAIEmbeddings(openai_api_key=api_key)

        #return cls(embeddings.embed_query, index, docstore, index_to_docstore_id)
        docsearch2 = FAISS(embeddings.embed_query, index_buffer, docstore_pickle, index_to_docstore_id_pickle)

        if sum == 1:
            kvalue=3
        else:
            kvalue=3

        docs = docsearch2.similarity_search(query,k=kvalue)
        print('docs:',docs)


        if sum==1:
            template = """We have an existing summary: {existing_answer}
                We have the opportunity to expand and refine the existing summary
                with some more context below.
                ------------
                {summaries}
                ------------
                Given the new context, create a refined detailed longer summary.
                """
        else:
            template = """Given the following extracted parts of a long document and a question, create a final answer.
            If you are not sure about the answer, just say that you are not sure before making up an answer.

            QUESTION: {question}
            =========
            {summaries}
            =========

            """

        #if language != 'en'
        #    template += """FINAL ANSWER IN """+language2

        print('tem',template)

        if sum==1:
            if language != 'en':
                template += """FINAL ANSWER IN """+language2
            c = asyncio.create_task(sync_to_async(getpaperabstract)(arxiv_id))
            paperabstract=await c
            PROMPT = PromptTemplate(template=template, input_variables=["summaries", "existing_answer"])
        else:
            if language != 'en' and not 'TRANSLATE' in query and not 'TRADUIRE' in query:
                template += """FINAL ANSWER IN """+language2
            PROMPT = PromptTemplate(template=template, input_variables=["summaries", "question"])

        chain = load_qa_with_sources_chain(llm, chain_type="stuff", prompt=PROMPT)

        with get_openai_callback() as cb:

            if sum==1:
                getresponse=chain({"input_documents": docs, "existing_answer": paperabstract}, return_only_outputs=False)
            else:
                getresponse=chain({"input_documents": docs, "question": query}, return_only_outputs=False)

            nbtokensused=cb.total_tokens

        print('nbtokensusedchatbot',nbtokensused)
        print('openai cost chatbot',nbtokensused*openaipricing("text-davinci-003"))

        #qa = ChatVectorDBChain.from_llm(llm, docs)
        #chat_history = []
        #getresponse = qa({"question": query, "chat_history": chat_history})


        #chain = load_qa_chain(llm, chain_type="stuff")
        #chain_type="map_reduce", return_map_steps=True)
        #getresponse=chain({"input_documents": docs, "question": query}, return_only_outputs=True)

        #getresponse=chain.run(input_documents=docs, question=query)
        print('getresponse',getresponse)
        print('getresponse2',getresponse['output_text'])

        finalresp=getresponse['output_text'].replace(':\n', '').rstrip().lstrip()

        #store the query and answer
        if sum != 1:
            c = asyncio.create_task(sync_to_async(getuserinst)(user))
            userinst = await c

            c = asyncio.create_task(sync_to_async(storeconversation)(arxiv_id,query,finalresp,userinst,language))
            await c

        return finalresp
    #else:
    #    return None


class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = StringIO()
        self.in_h1_tag = False

    def handle_starttag(self, tag, attrs):
        if tag == 'h1':
            self.in_h1_tag = True

    def handle_endtag(self, tag):
        if tag == 'h1':
            self.in_h1_tag = False

    def handle_data(self, d):
        if self.in_h1_tag:
            self.text.write('<b>{}</b>'.format(d))
        else:
            self.text.write(d)

    def get_data(self):
        return self.text.getvalue()

def strip_tags(html):
    s = MLStripper()
    s.feed(html)
    return s.get_data()

def summary_pdf2(arxiv_id,language):
    # Get the summary object from the database
    if ArxivPaper.objects.filter(arxiv_id=arxiv_id).exists():
        paper=ArxivPaper.objects.filter(arxiv_id=arxiv_id)[0]

        if SummaryPaper.objects.filter(paper=paper,lang=language).exists():
            sumpaper=SummaryPaper.objects.filter(paper=paper,lang=language)[0]
        elif SummaryPaper.objects.filter(paper=paper,lang='en').exists():
            sumpaper=SummaryPaper.objects.filter(paper=paper,lang='en')[0]
        else:
            sumpaper=''
            print('no summaries yet')

        print('paper',paper.title)
        # Generate the PDF file using reportlab
        #response = HttpResponse(content_type='application/pdf')
        #response = FileResponse(content_type='application/pdf')
        #response['Content-Disposition'] = f'attachment; filename="SummarizePaper-{str(arxiv_id)}.pdf"'
        filename="SummarizePaper-"+str(arxiv_id)+".pdf"
        response = HttpResponse(content_type="application/pdf")
        response['Content-Disposition'] = 'attachment; filename=%s' % filename
        # Create the PDF canvas
        from fpdf import FPDF, HTMLMixin
        #from io import BytesIO

        #import latexcodec
        #from pylatexenc.latex2text import LatexNodes2Text
        #buffer = BytesIO()

        #print('osss',os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed.ttf'))
        class MyPDF(FPDF, HTMLMixin):
            def __init__(self):
                super().__init__(orientation='P', unit='mm', format='A4')
                #self.add_font('DejaVu', '', 'font/DejaVuSansCondensed.ttf', uni=True)
                print('os.path.join(settings.BASE_DIR, "font", "DejaVuSansCondensed.ttf")',os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed.ttf'))
                self.add_font('DejaVu', '', "font/DejaVuSansCondensed.ttf", uni=True)
                self.add_font('DejaVu', 'B', "font/DejaVuSansCondensed-Bold.ttf", uni=True)
                self.add_font('DejaVu', 'I', "font/DejaVuSansCondensed-Oblique.ttf", uni=True)
                #self.add_font('DejaVu', 'B', os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed-Bold.ttf'), uni=True)

                self.add_page()
                #self.set_font("Arial", size=12)
                self.set_font("Helvetica", size=12)

            def header(self):
                self.set_font("DejaVu", "B", size=14)
                #self.set_font("Arial","B", size=14)
                self.cell(0, 10, "Made from SummarizePaper.com for arXiv ID: "+str(arxiv_id), 1, 0, "C")
                self.ln(20)

            def paperdet(self, title, text, url):
                #self.set_font("DejaVu", "I", size=12)
                self.set_font("Arial","I", size=12)
                self.cell(0, 10, "Title: "+title, 0, 1)
                self.set_font("Arial", size=10)

        pdf = MyPDF()

        if paper.link_doi:
            link=paper.link_doi
        else:
            link=paper.link_homepage

        pdf.paperdet(paper.title.strip(), paper.abstract.lstrip().rstrip(),str(link).strip())

        #out=pdf.output(dest='S')
        #print('resp',out)

        #pdf.output(BytesIO())

        #out = pdf.output()  # Probably what you want
        out=pdf.output(dest='S').encode('latin-1')
        #stream = BytesIO(byte_string)
        #buffer = BytesIO(out.encode('utf-8'))
        #response.write(buffer.getvalue())

        #print('buf',buffer)
        #pdf.output(buffer.getvalue())
        #pdf_bytes = buffer.getvalue()
        #buffer.close()
        #return pdf_bytes
        #pdf.output('filename.pdf', 'F')
        #response = HttpResponse(bytes(out), content_type='application/pdf')
        #response['Content-Disposition'] = "attachment; filename=myfilename.pdf"
        #return response

        return out

    else:
        print('no paper')

        return HttpResponseRedirect(reverse('arxividpage', args=(arxiv_id,)))

def summary_pdf(arxiv_id,language):
    # Get the summary object from the database
    if ArxivPaper.objects.filter(arxiv_id=arxiv_id).exists():
        paper=ArxivPaper.objects.filter(arxiv_id=arxiv_id)[0]

        if SummaryPaper.objects.filter(paper=paper,lang=language).exists():
            sumpaper=SummaryPaper.objects.filter(paper=paper,lang=language)[0]
        elif SummaryPaper.objects.filter(paper=paper,lang='en').exists():
            sumpaper=SummaryPaper.objects.filter(paper=paper,lang='en')[0]
        else:
            sumpaper=''
            print('no summaries yet')

        print('paper',paper.title)
        # Generate the PDF file using reportlab
        #response = HttpResponse(content_type='application/pdf')
        #response = FileResponse(content_type='application/pdf')
        #response['Content-Disposition'] = f'attachment; filename="SummarizePaper-{str(arxiv_id)}.pdf"'
        filename="SummarizePaper-"+str(arxiv_id)+".pdf"
        response = HttpResponse(content_type="application/pdf")
        response['Content-Disposition'] = 'attachment; filename=%s' % filename
        # Create the PDF canvas
        from fpdf import FPDF, HTMLMixin
        #import latexcodec
        #from pylatexenc.latex2text import LatexNodes2Text

        #print('osss',os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed.ttf'))
        class MyPDF(FPDF, HTMLMixin):
            def __init__(self):
                super().__init__(orientation='P', unit='mm', format='A4')
                #self.add_font('DejaVu', '', 'font/DejaVuSansCondensed.ttf', uni=True)
                print('os.path.join(settings.BASE_DIR, "font", "DejaVuSansCondensed.ttf")',os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed.ttf'))
                self.add_font('DejaVu', '', "font/DejaVuSansCondensed.ttf", uni=True)
                self.add_font('DejaVu', 'B', "font/DejaVuSansCondensed-Bold.ttf", uni=True)
                self.add_font('DejaVu', 'I', "font/DejaVuSansCondensed-Oblique.ttf", uni=True)
                #self.add_font('DejaVu', 'B', os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed-Bold.ttf'), uni=True)

                self.add_page()
                #self.set_font("Arial", size=12)
                self.set_font("Helvetica", size=12)


            def header(self):
                self.set_font("DejaVu", "B", size=14)
                #self.set_font("Arial","B", size=14)
                self.cell(0, 10, "Made from SummarizePaper.com for arXiv ID: "+str(arxiv_id), 1, 0, "C")
                self.ln(20)

            def paperdet(self, title, text, url):
                self.set_font("DejaVu", "I", size=12)
                #self.set_font("Arial","I", size=12)
                self.cell(0, 10, "Title: "+title, 0, 1)
                self.set_font("Arial", size=10)

                #self.multi_cell(0, 5, "Abstract: "+str(LatexNodes2Text().latex_to_text(text.encode("utf-8"))))
                #text_str = latex2text(text)
                #self.set_font("DejaVu", "B", size=14)

                #latex_converter = LatexNodes2Text()

                # Convert the LaTeX code to plain text
                #print('latex_converter.latex_to_text(text)',latex_converter.latex_to_text(text))
                #text_str = latex_converter.latex_to_text(text)#.encode('latin-1')#.encode('utf-8')

                self.multi_cell(0, 5, "Abstract: "+text)

                #self.set_font("Arial", "I", size=10)
                #self.cell(0, 10, "URL: ", 0, 0)

                # Get the x and y positions of the current cell

                #x, y = self.get_x(), self.get_y()

                # Set the font and color for the link
                self.set_text_color(0, 0, 255)
                self.set_font("Arial", "U", size=10)

                # Set the link target
                #link = self.add_link()
                self.write(10, url, url)

                # Reset the font and color
                self.set_font("Arial", size=10)
                self.set_text_color(0, 0, 0)

                # Add a line break
                self.ln(10)


            def section(self, title, text):
                self.set_font("DejaVu", "B", size=12)
                #self.set_font("Arial","B", size=12)
                self.cell(0, 10, title, 0, 1)
                self.set_font("DejaVu", size=12)
                #self.set_font("Arial", size=12)
                h1_text = re.search(r'<b>(.*?)</b>', text)
                if h1_text:
                    h1_text = h1_text.group(1)
                    #self.set_font("Arial","I", size=12)
                    self.set_font("DejaVu","I", size=12)
                    self.cell(0, 10, h1_text, 0, 1)
                    self.set_font("DejaVu", size=11)
                    #self.set_font("Arial", size=11)
                    # Remove the extracted h1 text from the text to avoid duplication
                    text = text.replace(f"<b>{h1_text}</b>", "")
                self.multi_cell(0, 7, text)
                self.ln(10)

            def sectionhtml(self, title, html):
                #self.set_font("DejaVu", "B", size=12)
                self.set_font("Arial","B", size=12)
                self.cell(0, 10, title, 0, 1)
                self.set_font("Arial", size=11)
                texthtml="<font color='#000000'>"+html+"</font>"
                print('te',texthtml)
                self.write_html(texthtml)

                self.ln(10)



        pdf = MyPDF()
        #pdf.add_font('DejaVu', '', os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed.ttf'), uni=True)
        #pdf.add_font('DejaVu', 'B', os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed-Bold.ttf'), uni=True)
        #pdf.add_font('DejaVu', 'I', os.path.join(settings.BASE_DIR, "font", 'DejaVuSansCondensed-Oblique.ttf'), uni=True)


        #print('fonts:',pdf.get_font_family())



        # Add the first summary section to the document
        if paper.link_doi:
            link=paper.link_doi
        else:
            link=paper.link_homepage

        pdf.paperdet(paper.title.strip(), paper.abstract.lstrip().rstrip(),str(link).strip())

        if sumpaper:
            if sumpaper.summary:
                print('in comp sum')
                pdf.section("Comprehensive Summary", sumpaper.summary.lstrip().rstrip())

            # Add the second summary section to the document
            notesarr=''
            if sumpaper.notes:
                notes = sumpaper.notes.replace('•','')
                print('rrrr',notes)

                try:
                    notesarr = ast.literal_eval(notes)
                except ValueError:
                    # Handle the error by returning a response with an error message to the user
                    return HttpResponse("Invalid input: 'notes' attribute is not a valid Python literal.")

            notestr=''
            if notesarr:
                for note in notesarr:
                    notestr+='-'+note+'\n'
                    print('n',note)

            if sumpaper.notes:
                pdf.section("Key Points", notestr)

            if sumpaper.lay_summary:
                pdf.section("Layman's summary", sumpaper.lay_summary.lstrip().rstrip())

            if sumpaper.blog:
                print('pap',sumpaper.blog)
                #pdf.sectionhtml("Blog Article", paper.blog)

                pdf.section("Blog Article", strip_tags(sumpaper.blog.lstrip().rstrip()))

        #pdf.section("Blog Article", notestr)

        #pdf.section("Key Points", summary_2.encode('latin-1', 'replace').decode('latin-1'))

        # Save the PDF file
        out=pdf.output(dest='S').encode('latin-1')
        print('resp')

        return out

    else:
        print('no paper')

        return HttpResponseRedirect(reverse('arxividpage', args=(arxiv_id,)))


def update_arxiv_paper(arxiv_id,summary):

    paper, created = ArxivPaper.objects.update_or_create(
        arxiv_id=arxiv_id,
        defaults={'summary': summary}
    )
    return paper, created



def summarizer(arxiv_id):
    total_steps = 10
    for i in range(total_steps):
        # Perform some action to update the loading message and progress

        progress = 100 * (i + 1) / total_steps
        print('jk',progress)
        #time.sleep(0.1)

        yield progress

async def extract_text_from_pdf(pdf_filename):
    # Open the PDF file
    with open(pdf_filename, 'rb') as file:
        # Create a PDF resource manager object that stores shared resources
        resource_manager = PDFResourceManager()

        # Create a string buffer object for text extraction
        text_io = StringIO()

        # Create a text converter object
        text_converter = TextConverter(resource_manager, text_io, laparams=LAParams())

        # Create a PDF page interpreter object
        page_interpreter = PDFPageInterpreter(resource_manager, text_converter)

        # Process each page in the PDF file
        for page in PDFPage.get_pages(file, caching=True, check_extractable=True):
            page_interpreter.process_page(page)
            text = text_io.getvalue()


        end = text.find("References")
        end2 = text.find("REFERENCES")
        end3 = text.find("Acknowledgements")
        end4 = text.find("ACKNOWLEDGEMENTS")

        print('end,end2,end3,end4',end,end2,end3,end4)
        numbers=[end,end2,end3,end4]
        min_positive = float('inf')
        for number in numbers:
            if number > 0 and number < min_positive:
                min_positive = number

        if min_positive != float('inf'):
            print("The smallest positive number is", min_positive)
            endf=min_positive
        else:
            print("There are no positive numbers in the list")
            endf=-1

        print('abs:',text[0:endf].strip())
        textlim=text[0:endf].strip()
        # Close the text buffer and the text converter

        text_io.close()
        text_converter.close()


        # Return the extracted text
        return [textlim,text]


async def send_message_now(arxiv_group_name,message):
    print('in sendmesnow')

    await channel_layer.group_send(
        arxiv_group_name, {"type": "progress_text_update", "message": message}
    )



async def summarize_book(arxiv_id, language, book_text, api_key):
    endpoint = "https://api.openai.com/v1/engines/"+model+"/completions"

    message={}
    arxiv_group_name="ar_%s" % arxiv_id
    # Split the book into chunks of at most 4096 tokens
    print("len(book_text)",len(book_text))

    if method=="Quentin":
        chunk_size = 4096
        chunks = [book_text[i:i+chunk_size] for i in range(0, len(book_text), chunk_size)]

        # Send each chunk to the API for summarization
        summarized_chunks = []
        for chunk in chunks:
            prompt = f"summarize the following text in 100 words: {chunk}"
            print("prompt:",prompt)
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
            response = requests.post(endpoint, headers=headers, json={"prompt": prompt, "max_tokens": 400, "temperature": temp, "n":1, "stop":None})

            try:
                print('in try1')
                if response.status_code != 200:
                    print("in1 ! 200")
                    raise Exception(f"Failed to summarize text: {response.text}")
            except Exception as e:
                print('in redirect1',str(e))
                # Redirect to the arxividpage and pass the error message
                return {
                    "error_message": str(e),
                }
                #return redirect('arxividpage', arxiv_id=arxiv_id, error_message="e1")#str(e))
                #return render(request, "summarizer/arxividpage.html", stuff_for_frontend)


            #if response.status_code != 200:
            #    raise Exception(f"Failed to summarize chunk: {response.text}")

            summarized_chunks.append(response.json()["choices"][0]["text"])
            print('yo:\n',response.json()["choices"][0]["text"])

        # Concatenate the summarized chunks and send the result to the API for further summarization

        print("beeeefffffooooooorrreee1")
        message["progress"] = 35
        message["loading_message"] = "Summarizing in progress..."
        c=asyncio.create_task(send_message_now(arxiv_group_name,message))

        #c=asyncio.create_task(channel_layer.group_send(arxiv_group_name, {"type": "progress_text_update", "message": message}))
        await c
        #time.sleep(10.)
        print("afffffteeeeeeeerrrrrdsdrrreee1")

        summarized_text = " ".join(summarized_chunks)
        print("len(summarized_text)",len(summarized_text))
        print('summarized_text',summarized_text)

        cont=1
        final_summarized_text=summarized_text
        i=0
        while cont==1:
            print('iiiiiiiiiiiiiiiii:\n',i)
            chunks2 = [final_summarized_text[i:i+chunk_size] for i in range(0, len(final_summarized_text), chunk_size)]

            summarized_chunks2 = []
            jj=0
            for chunk2 in chunks2:
                print('jjjjjjjjjjjjjj:\n',jj)

                prompt2 = f"Summarize the following text from a research article in 300 words: {chunk2}"
                headers2 = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}"
                }
                response2 = requests.post(endpoint, headers=headers2, json={"prompt": prompt2, "max_tokens": 500, "temperature": temp, "n":1, "stop":None})

                #if response2.status_code != 200:
                #    raise Exception(f"Failed to summarize text: {response2.text}")
                    #it happens sometimes so to be treated...
                try:
                    print('in try')
                    if response2.status_code != 200:
                        print("in ! 200")
                        raise Exception(f"Failed to summarize text: {response2.text}")
                except Exception as e:
                    print('in redirect')
                    # Redirect to the arxividpage and pass the error message
                    return {
                        "error_message": str(e),
                    }                #return render(request, "summarizer/arxividpage.html", stuff_for_frontend)

                summarized_chunks2.append(response2.json()["choices"][0]["text"])
                jj+=1

            print('len summarized_chunks2',len(summarized_chunks2))


            print("beeeefffffooooooorrreee2")
            message["progress"] = 50
            message["loading_message"] = "Extracting key points..."
            c=asyncio.create_task(send_message_now(arxiv_group_name,message))

            #c=asyncio.create_task(channel_layer.group_send(arxiv_group_name, {"type": "progress_text_update", "message": message}))
            await c
            #time.sleep(10.)
            print("afffffteeeeeeeerrrrrdsdrrreee2")


            summarized_text2 = " ".join(summarized_chunks2)
            print('\nsummmmmmmmmmmmmmm\n',summarized_text2)
            if len(summarized_chunks2)==1:
            #if len(summarized_text2)<chunk_size:
                cont=0
                print('yes:\n',len(summarized_chunks2))
            else:
                print('no:\n',len(summarized_chunks2))
                final_summarized_text=summarized_text2#summarized_chunks2
            i+=1

        final_summarized_text = summarized_text2#response2.json()["choices"][0]["text"]
        print('yoyo:\n',final_summarized_text)

    elif method=='fromembeddings':
        print('from embeddings')
        query="Create a long detailed summary of the paper, preserve important details"
        c=asyncio.create_task(chatbot(arxiv_id,language,query,api_key))
        #c=asyncio.create_task(utils.chatbot("my_pdf.pdf"))
        final_summarized_text =await c
        print('apres final_summarized_text',final_summarized_text)
    elif method=='fromembeddingsandabstract':
        print('from embeddings2')
        query="Create a long detailed summary of the paper"

        c=asyncio.create_task(chatbot(arxiv_id,language,query,api_key,sum=1))
        #c=asyncio.create_task(utils.chatbot("my_pdf.pdf"))
        final_summarized_text = await c
        print('before nltk final_summarized_text',final_summarized_text)


        # Download the nltk punkt tokenizer if necessary
        nltk.download('punkt')


        # Split the summary into individual sentences
        sentences = nltk.sent_tokenize(final_summarized_text)

        # Filter out sentence fragments
        final_summarized_text = [s for s in sentences if s.endswith((".", "!", "?"))]

        # Print the full sentences
        #for s in final_summarized_text:
        #    print(s)
        final_summarized_text = ' '.join(final_summarized_text)

        print('await')
        #await asyncio.sleep(50)



        #final_summarized_text=finalise_and_keywordsb
        print('apres final_summarized_text',final_summarized_text)

    else:
        #llm = OpenAI(temperature=0,openai_api_key=api_key)
        modelforsummarizing="text-davinci-003"#"text-curie-001"
        #text-davinci-003#4000 tokens#chunk_size=2000#max_tokens=1000
        #text-curie-001#2048 tokens
        #text-babbage-001#2048 tokens
        #text-ada-001#2048 tokens
        llm = OpenAI(model_name=modelforsummarizing,max_tokens=1000,best_of=1,n=1,temperature=0.3,openai_api_key=api_key)
        #best_of=2,streaming=True
        li = get_language_info(language)
        language2 = li['name']
        print('language2',language2)
        #from transformers import GPT2TokenizerFast
        #tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")

        text_splitter = CharacterTextSplitter.from_tiktoken_encoder(separator = "\n\n", chunk_size=2000, chunk_overlap=0)
        texts = text_splitter.split_text(book_text)

        for text in texts:
            print('text:------------------',text)

        '''
        text_splitter = CharacterTextSplitter(
        separator = "\n",
        chunk_size = 1000,
        chunk_overlap  = 10,
        length_function = len,
        )
        texts = text_splitter.split_text(book_text)
        for text in texts:
            print('text:------------------',text)
        '''
        print('tttettxtxtxtxtxtxtxtttzetet',texts)
        #docs = [Document(page_content=t) for t in texts[:3]]
        docs = [Document(page_content=t) for t in texts]

        print('docs---------------',texts[:3])

        prompt_template = """Create a long detailed summary of the following text:
        {text}

        LONG DETAILED SUMMARY:

        """

        if language != 'en':
            prompt_template += """TRANSLATE THE ANSWER IN """+language2

        print('prompt_template',prompt_template)

        PROMPT = PromptTemplate(template=prompt_template, input_variables=["text"])
        #chain = load_summarize_chain(llm, chain_type="stuff", prompt=PROMPT)
        chain = load_summarize_chain(llm, chain_type="map_reduce", return_intermediate_steps=True, map_prompt=PROMPT, combine_prompt=PROMPT)

        with get_openai_callback() as cb:

            res=chain({"input_documents": docs}, return_only_outputs=True)
            nbtokensused=cb.total_tokens

        print('nbtokensused',nbtokensused)
        print('openai cost',nbtokensused*openaipricing(modelforsummarizing))

        #chain.run(docs)
        print('res',res)
        #input("Press Enter to continueb...")

        final_summarized_text = res['output_text']

    return final_summarized_text


async def finalise_and_keywords(arxiv_id, language, summary, api_key):
    endpoint = "https://api.openai.com/v1/engines/"+model+"/completions"
    li = get_language_info(language)
    language2 = li['name']
    print('language2',language2)

    #Extract the most important key points from the following text
    prompt3b = """
        Improve the text and remove all unfinished sentences from: {}

        Moreover, create 5 keywords from the text and write them at the beginning of the output between <kd> </kd> tags

    """.format(summary)

    print('finalise sum',prompt3b)
    if language != 'en':
        prompt3b += "TRANSLATE THE ANSWER IN "+language2


    headers3b = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    response3b = requests.post(endpoint, headers=headers3b, json={"prompt": prompt3b,"frequency_penalty":0.6, "presence_penalty":0.6,"max_tokens": 800, "temperature": temp, "n":1, "stop":None})

    try:
        print('in try2b')
        if response3b.status_code != 200:
            print("in2b ! 200")
            raise Exception(f"Failed to summarize text2b: {response3b.text}")
    except Exception as e:
        print('in redirect2b')
        # Redirect to the arxividpage and pass the error message
        return {
            "error_message": str(e),
        }        #return render(request, "summarizer/arxividpage.html", stuff_for_frontend)

    #if response3.status_code != 200:
    #    raise Exception(f"Failed to extract key points: {response3.text}")

    print('icccciiiiiib',response3b.json()["choices"][0]["text"])

    finalise_and_keywords2 = response3b.json()["choices"][0]["text"].rstrip().lstrip()
    print('finalise_and_keywords',finalise_and_keywords2)

    # Find the text between the <keywords> tags
    match = re.search(r"<kd>(.*?)</kd>", finalise_and_keywords2)
    if match:
        # Extract the text between the tags
        keywords_text = match.group(1)

        # Remove the tags and the extracted text from the original text
        finalise_and_keywords2 = re.sub(r"<kd>.*?</kd>", "", finalise_and_keywords2)

        # Print the extracted text and the text without the keywords
        print("Keywords: {}".format(keywords_text))
        print("Text without keywords: {}".format(finalise_and_keywords2))
        #save the keywords

    else:
        print("No keywords found in text")
        keywords_text=''




    sentences = nltk.sent_tokenize(finalise_and_keywords2)

    # Filter out sentence fragments
    finalise_and_keywords2 = [s for s in sentences if s.endswith((".", "!", "?"))]

    # Print the full sentences
    #for s in final_summarized_text:
    #    print(s)
    finalise_and_keywords2 = ' '.join(finalise_and_keywords2)
    print('simple_sum after',finalise_and_keywords2)


    '''#problem cuz keypoints do not finish with dots
    sentences = nltk.sent_tokenize(' '.join(key_points))

    # Filter out sentence fragments
    sentences = [s for s in sentences if s.endswith((".", "!", "?"))]

    # Join the remaining sentences into a single string
    key_points = sentences
    print('key_points after',key_points)
    '''

    return [finalise_and_keywords2,keywords_text]

async def extract_key_points(arxiv_id, language, summary, api_key):
    endpoint = "https://api.openai.com/v1/engines/"+model+"/completions"
    li = get_language_info(language)
    language2 = li['name']
    print('language2',language2)

    #Extract the most important key points from the following text
    prompt3 = f"Extract the most important key points from the following text and use bullet points for each of them: {summary}"
    print('key sum',prompt3)
    if language != 'en':
        prompt3 += "TRANSLATE THE ANSWER IN "+language2


    headers3 = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    response3 = requests.post(endpoint, headers=headers3, json={"prompt": prompt3, "max_tokens": 500,"frequency_penalty":0.6, "presence_penalty":0.6, "temperature": temp, "n":1, "stop":None})

    try:
        print('in try2')
        if response3.status_code != 200:
            print("in2 ! 200")
            raise Exception(f"Failed to summarize text2: {response3.text}")
    except Exception as e:
        print('in redirect2')
        # Redirect to the arxividpage and pass the error message
        return {
            "error_message": str(e),
        }        #return render(request, "summarizer/arxividpage.html", stuff_for_frontend)

    #if response3.status_code != 200:
    #    raise Exception(f"Failed to extract key points: {response3.text}")

    print('icccciiiiii',response3.json()["choices"][0]["text"])

    key_points = response3.json()["choices"][0]["text"].rstrip().lstrip().strip().split("\n")
    print('key_points',key_points)

    '''#problem cuz keypoints do not finish with dots
    sentences = nltk.sent_tokenize(' '.join(key_points))

    # Filter out sentence fragments
    sentences = [s for s in sentences if s.endswith((".", "!", "?"))]

    # Join the remaining sentences into a single string
    key_points = sentences
    print('key_points after',key_points)
    '''

    return key_points

async def extract_simple_summary(arxiv_id, language, keyp, api_key):
    endpoint = "https://api.openai.com/v1/engines/"+model+"/completions"
    li = get_language_info(language)
    language2 = li['name']
    print('language2',language2)

    prompt4 = """
        Summarize the following key points in 5 sentences for a six year old kid: {}

        Give definitions for the 3 most important words in the summary.

        Definitions:

    """.format(keyp)

    #prompt4 = f"Summarize the following key points in 5 sentences for a six year old kid: {keyp}"
    #prompt4 += "Skip 3 lines and Give definitions for the 3 most important words in the summary."

    if language != 'en':
        prompt4 += "TRANSLATE THE ANSWER IN "+language2

    headers4 = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    response4 = requests.post(endpoint, headers=headers4, json={"prompt": prompt4, "max_tokens": 300,"frequency_penalty":0.6, "presence_penalty":0.6, "temperature": temp, "n":1, "stop":None})

    try:
        print('in try3')
        if response4.status_code != 200:
            print("in3 ! 200")
            raise Exception(f"Failed to summarize text: {response4.text}")
    except Exception as e:
        print('in redirect3')
        # Redirect to the arxividpage and pass the error message
        return {
            "error_message": str(e),
        }
        #return redirect('arxividpage', arxiv_id=arxiv_id, error_message="e1")
        #return render(request, "summarizer/arxividpage.html", stuff_for_frontend)

    #if response4.status_code != 200:
    #    raise Exception(f"Failed to extract key points: {response4.text}")

    simple_sum = response4.json()["choices"][0]["text"]#.strip().split("\n")
    print('simple_sum',simple_sum)
    # Split the summary into individual sentences
    sentences = nltk.sent_tokenize(simple_sum)

    # Filter out sentence fragments
    simple_sum = [s for s in sentences if s.endswith((".", "!", "?"))]

    # Print the full sentences
    #for s in final_summarized_text:
    #    print(s)
    simple_sum = ' '.join(simple_sum)
    print('simple_sum after',simple_sum)

    return simple_sum.rstrip().lstrip()

async def extract_blog_article(arxiv_id, language, summary, api_key):
    endpoint = "https://api.openai.com/v1/engines/"+model+"/completions"
    li = get_language_info(language)
    language2 = li['name']
    print('language2',language2)

    prompt5 = f"Create a detailed blog article in html about this research paper: {summary}"
    if language != 'en':
        prompt5 += "TRANSLATE THE ANSWER IN "+language2

    headers5 = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    response5 = requests.post(endpoint, headers=headers5, json={"prompt": prompt5,"frequency_penalty":0.8, "presence_penalty":0.8, "max_tokens": 1500, "temperature": temp, "n":1, "stop":None})

    try:
        print('in try5')
        if response5.status_code != 200:
            print("in5 ! 200")
            raise Exception(f"Failed to summarize text: {response5.text}")
    except Exception as e:
        print('in redirect5')
        # Redirect to the arxividpage and pass the error message
        return {
            "error_message": str(e),
        }
        #return redirect('arxividpage', arxiv_id=arxiv_id, error_message="e1")
        #return render(request, "summarizer/arxividpage.html", stuff_for_frontend)

    #if response4.status_code != 200:
    #    raise Exception(f"Failed to extract key points: {response4.text}")

    blog_article = response5.json()["choices"][0]["text"]#.strip().split("\n")
    print('blog article',blog_article)
    sentences = nltk.sent_tokenize(blog_article)

    # Filter out sentence fragments
    blog_article = [s for s in sentences if s.endswith((".", "!", "?"))]

    # Print the full sentences
    #for s in final_summarized_text:
    #    print(s)
    blog_article = ' '.join(blog_article)
    print('blog article after',blog_article)

    return blog_article.rstrip().lstrip()

def arxiv_search(query):

    #url = 'http://export.arxiv.org/api/query?search_query=ti:'+query+'&start=0&max_results=1&sortBy=lastUpdatedDate&sortOrder=ascending'
    query = urllib.parse.quote(query)
    # Define the API endpoint URL
    url = f"http://export.arxiv.org/api/query?search_query=all:{query}&start=0&max_results=25"

    response = urllib.request.urlopen(url)
    data = response.read()
    print('data',data.decode('utf-8'))
    root = ElementTree.fromstring(data)

    # find and modify the value of an element

    ns = {'ns0': 'http://www.w3.org/2005/Atom','ns1':'http://a9.com/-/spec/opensearch/1.1/','ns2':'http://arxiv.org/schemas/atom'} # add more as needed
    tit=root.find('ns0:title', ns).text

    # Extract the authors, title, and abstract

    #check if exists
    exist=0
    authors=""
    authors_array=[]
    affiliation=""
    affiliation_array=[]
    link_hp=""
    link_hp_array=[]
    title=""
    title_array=[]
    link_doi=""
    link_doi_array=[]
    abstract=""
    abstract_array=[]
    cat=""
    cat_array=[]
    updated=""
    updated_array=[]
    published=""
    published_array=[]
    journal_ref=""
    journal_ref_array=[]
    comments=""
    comments_array=[]
    papers=[]

    for entry in root.findall("ns0:entry",ns):
        if entry.find("ns0:title",ns) is not None:
            exist=1
            print('exist',exist)

            if exist == 1:

                #for entry in root.findall("ns0:entry",ns):
                authors = []
                affiliation = []
                title = ""
                abstract = ""
                for author in entry.findall("ns0:author",ns):
                    authors.append(author.find("ns0:name",ns).text)
                    print('test',authors)
                    if author.find("ns2:affiliation",ns) is not None:
                        print('aff',author.find("ns2:affiliation",ns).text)
                        affiliation.append(author.find("ns2:affiliation",ns).text)
                    else:
                        affiliation.append('')
                authors_array.append(authors)
                affiliation_array.append(affiliation)

                link_hp = entry.find("ns0:id",ns).text
                link_hp_array.append(link_hp)
                title = entry.find("ns0:title",ns).text
                title_array.append(title)
                link_doi = entry.find("ns0:link",ns).attrib['href']
                link_doi_array.append(link_doi)
                abstract = entry.find("ns0:summary",ns).text
                abstract_array.append(abstract)
                if entry.find("ns2:primary_category",ns) is not None:
                    cat = entry.find("ns2:primary_category",ns).attrib['term']
                cat_array.append(cat)
                updated = entry.find("ns0:updated",ns).text
                updated_array.append(updated)
                if entry.find("ns0:published",ns) is not None:
                    published = entry.find("ns0:published",ns).text
                published_array.append(published)
                #print('kllll',entry.find("ns2:journal_ref",ns))
                if entry.find("ns2:journal_ref",ns) is not None:
                    journal_ref = entry.find("ns2:journal_ref",ns).text
                else:
                    journal_ref = ''
                journal_ref_array.append(journal_ref)
                if entry.find("ns2:comment",ns) is not None:
                    comments = entry.find("ns2:comment",ns).text
                else:
                    comments = ''
                comments_array.append(comments)

                arxiv_id = re.search(r'/(\d+\.\d+)', link_hp).group(1)
                papers.append({'arxiv_id':arxiv_id,'title': title, 'authors': authors, 'link':link_hp,'category':cat})

    print('all lot',authors_array)
    print('aff arr',affiliation_array)

    return papers

def get_arxiv_metadata(arxiv_id):
    print('aaa',arxiv_id)
    if '--' in arxiv_id:
        print('arxiv_id1',arxiv_id)
        arxiv_id=arxiv_id.replace('--','/')
        print('arxiv_id2',arxiv_id)

    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    response = requests.get(url)
    #if response.status_code != 200:
    #    raise Exception(f"Failed to retrieve data: {response.text}")

    arxiv_id2= re.sub(r'v\d+$', '', arxiv_id)
    print('2',arxiv_id2)  # need to remove v1 or v2 as license is not found otherwise
    url2=f"http://export.arxiv.org/oai2?verb=GetRecord&identifier=oai:arXiv.org:{arxiv_id2}&metadataPrefix=arXiv"
    response2 = requests.get(url2)

    try:
        print('in try arxiv')
        if response.status_code != 200:
            print("in ! 200 arxiv")
            raise Exception(f"Failed to retrieve data: {response.text}")
    except Exception as e:
        print('in redirect arxiv')

    try:
        print('in try arxiv2')
        if response2.status_code != 200:
            print("in ! 200 arxiv2")
            raise Exception(f"Failed to retrieve data2: {response2.text}")
    except Exception as e:
        print('in redirect arxiv2')
        # Redirect to the arxividpage and pass the error message
        #return {
        #    "error_message": str(e),
        #}
        #return redirect('arxividpage', arxiv_id=arxiv_id, error_message="e0")
        #return render(request, "summarizer/arxividpage.html", stuff_for_frontend)


    data = response.text
    print('data',data)

    data2 = response2.text
    print('data2',data2)

    # Parse the XML response
    root = ElementTree.fromstring(data)

    root2 = ElementTree.fromstring(data2)

    # find and modify the value of an element

    #arxiv = root2.find(".//{http://arxiv.org/OAI/arXiv/}arXiv")
    ns = {'arxiv': 'http://arxiv.org/OAI/arXiv/'}
    arxiv = root2.find('.//arxiv:arXiv', ns)
    license_value=''

    if arxiv is not None:
        license_tag = arxiv.find('arxiv:license', ns)
        #license_tag = arxiv.find('.//license')

        if license_tag is not None:
            license_value = license_tag.text
            print(f"The license tag value is: {license_value}")
        else:
            #license_value=''
            print("The license tag was not found.")

    #for arxiv in root2.findall('.//{http://arxiv.org/OAI/arXiv/}arXiv'):
    #    print('a',arxiv)
    #    license_tag = arxiv.find('license').text
    #    print(f"The value of the 'license' tag is: {license_tag}")



    ns = {'ns0': 'http://www.w3.org/2005/Atom','ns1':'http://a9.com/-/spec/opensearch/1.1/','ns2':'http://arxiv.org/schemas/atom'} # add more as needed
    tit=root.find('ns0:title', ns).text

    # Extract the authors, title, and abstract

    #check if exists
    exist=0
    authors=""
    affiliation=""
    link_hp=""
    title=""
    link_doi=""
    abstract=""
    cat=""
    updated=""
    published=""
    journal_ref=""
    comments=""

    for entry in root.findall("ns0:entry",ns):
        if entry.find("ns0:title",ns) is not None:
            exist=1
            print('exist',exist)


    if exist == 1:
        authors = []
        affiliation = []
        title = ""
        abstract = ""
        for entry in root.findall("ns0:entry",ns):
            for author in entry.findall("ns0:author",ns):
                authors.append(author.find("ns0:name",ns).text)
                print('test')
                if author.find("ns2:affiliation",ns) is not None:
                    print('aff',author.find("ns2:affiliation",ns).text)
                    affiliation.append(author.find("ns2:affiliation",ns).text)
                else:
                    affiliation.append('')

            link_hp = entry.find("ns0:id",ns).text
            title = entry.find("ns0:title",ns).text
            link_doi = entry.find("ns0:link",ns).attrib['href']
            abstract = entry.find("ns0:summary",ns).text
            if entry.find("ns2:primary_category",ns) is not None:
                cat = entry.find("ns2:primary_category",ns).attrib['term']
            updated = entry.find("ns0:updated",ns).text
            if entry.find("ns0:published",ns) is not None:
                published = entry.find("ns0:published",ns).text
            #print('kllll',entry.find("ns2:journal_ref",ns))
            if entry.find("ns2:journal_ref",ns) is not None:
                journal_ref = entry.find("ns2:journal_ref",ns).text
            else:
                journal_ref = ''
            if entry.find("ns2:comment",ns) is not None:
                comments = entry.find("ns2:comment",ns).text
            else:
                comments = ''


    return [exist, authors, affiliation, link_hp, title, link_doi, abstract, cat, updated, published, journal_ref, comments,license_value]

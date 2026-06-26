import streamlit as st
from model import predict_ticket
from textblob import TextBlob
import pandas as pd
import matplotlib.pyplot as plt

st.title("AI Customer Complaint Intelligence System")

text = st.text_input("Enter Customer Complaint")

if st.button("Analyze"):

    category = predict_ticket(text)

    sentiment = TextBlob(text).sentiment.polarity

    if sentiment > 0:
        sentiment_result = "Positive"
    elif sentiment < 0:
        sentiment_result = "Negative"
    else:
        sentiment_result = "Neutral"

    st.write("Predicted Category:", category)
    st.write("Sentiment:", sentiment_result)

df = pd.read_csv("data.csv")

st.subheader("Complaint Category Distribution")

fig, ax = plt.subplots()

df["category"].value_counts().plot(kind="bar", ax=ax)

st.pyplot(fig)

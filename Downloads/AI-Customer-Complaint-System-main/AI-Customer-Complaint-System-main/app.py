import streamlit as st
from model import predict_ticket
from textblob import TextBlob
import pandas as pd
import matplotlib.pyplot as plt

st.set_page_config(
    page_title="AI Customer Complaint Intelligence System",
    page_icon="🤖",
    layout="wide"
)

st.markdown("""
<style>
.main-title{
    background:linear-gradient(90deg,#2563EB,#7C3AED);
    padding:20px;
    border-radius:12px;
    color:white;
    text-align:center;
    margin-bottom:20px;
}
.metric-box{
    padding:18px;
    border-radius:12px;
    background:#F3F4F6;
    text-align:center;
    box-shadow:0 2px 8px rgba(0,0,0,0.1);
}
.footer{
    text-align:center;
    color:gray;
    margin-top:40px;
}
</style>
""", unsafe_allow_html=True)

with st.sidebar:

    st.title("🤖 AI Complaint System")

    st.success("Version 1.0")

    st.markdown("---")

    st.subheader("🛠 Technology Stack")

    st.markdown("""
- 🐍 Python
- ⚡ Streamlit
- 🤖 Scikit-learn
- 💬 TextBlob
- 📊 Pandas
- 📈 Matplotlib
""")

    st.markdown("---")

    st.subheader("✨ Features")

    st.markdown("""
- ✅ Complaint Classification
- ✅ Sentiment Analysis
- ✅ Interactive Dashboard
- ✅ Data Visualization
""")

st.markdown("""
<div class="main-title">
<h1>🤖 AI Customer Complaint Intelligence System</h1>
<p>Machine Learning & Natural Language Processing for Intelligent Complaint Analysis</p>
</div>
""", unsafe_allow_html=True)

st.markdown("""
Analyze customer complaints using **Machine Learning** and **Natural Language Processing (NLP).**

The application predicts:

- 📂 Complaint Category
- 😊 Customer Sentiment
- 📊 Complaint Category Distribution
""")

st.divider()

st.subheader("📝 Analyze Complaint")

text = st.text_area(
    "",
    placeholder="Example: I paid for the premium subscription but it was not activated."
)

if st.button("🚀 Analyze Complaint", use_container_width=True):

    if text.strip() == "":
        st.warning("Please enter a customer complaint.")

    else:

        category = predict_ticket(text)

        polarity = TextBlob(text).sentiment.polarity

        if polarity > 0:
            sentiment = "Positive"
        elif polarity < 0:
            sentiment = "Negative"
        else:
            sentiment = "Neutral"

        col1, col2 = st.columns(2)

        with col1:
            st.metric(
                label="📂 Complaint Category",
                value=category
            )

        with col2:
            st.metric(
                label="😊 Customer Sentiment",
                value=sentiment
            )

st.divider()

st.subheader("📊 Complaint Category Distribution")

df = pd.read_csv("data.csv")

colors = [
    "#2563EB",
    "#10B981",
    "#F59E0B",
    "#EF4444",
    "#8B5CF6",
    "#06B6D4",
    "#EC4899"
]

fig, ax = plt.subplots(figsize=(9,5))

df["category"].value_counts().plot(
    kind="bar",
    color=colors,
    edgecolor="black",
    ax=ax
)

ax.set_xlabel("Category", fontsize=12)
ax.set_ylabel("Count", fontsize=12)
ax.set_title("Customer Complaint Categories", fontsize=16)

plt.xticks(rotation=25)

st.pyplot(fig)

st.divider()

st.markdown("""
<div class="footer">

## 👨‍💻 Developed by Uday Kiran

**AI Customer Complaint Intelligence System**

Python • Machine Learning • NLP • Streamlit

📧 gangasaniudayreddy99@gmail.com

</div>
""", unsafe_allow_html=True)
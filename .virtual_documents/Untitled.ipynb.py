import tkinter
from PIL import Image,ImageTk
win = tkinter.Tk()
pilImage = Image.open("/Users/bluesky/Desktop/Surakarta_Raw/resource/UIBegin.jpeg")
img = pilImage.resize((600, 500), Image.ANTIALIAS)
tkImage = ImageTk.PhotoImage(image=img)
label = tkinter.Label(win, image=tkImage, width=600, height=500)
label.place(x=120, y=20)
win.mainloop()




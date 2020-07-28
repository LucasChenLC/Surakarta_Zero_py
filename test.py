import tkinter
from PIL import Image, ImageTk

win = tkinter.Tk()
win.geometry('800x600')
pilImage = Image.open("UI_Image/board.jpg")
tkImage = ImageTk.PhotoImage(image=pilImage)
label = tkinter.Label(win, image=tkImage, width=800, height=600)
label.place(x=0, y=0)
win.mainloop()
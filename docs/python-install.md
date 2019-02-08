# How to install Python 3

You have to have Python 3 to migrate to Braiins OS and run all the `*.py` scripts. This guide describes how to install Python 3 and other required tools.

## Linux

Python is usually **available by default**, however, you might need to install `virtualenv`. *Please refer to your distribution documentation for more details.*

## macOS

In order to get Python and Virtualenv, Homebrew is required. Homebrew requires Xcode. Install these tools using the following commands.


```bash
# install xcode
xcode-select --install

# install homebrew
/usr/bin/ruby -e "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/master/install)"

# install python3
brew install python3

# install virtualenv
pip3 install virtualenv
```

## Windows

On Windows, there are **two options** available:

1) Install *Ubuntu on Windows* and follow the standard Linux commands and procedures (recommended).

2) Install Python directly on Windows. In this case, some of the commands described in the Braiins OS documentation might not work or may require adjustments.

### Option 1: Ubuntu on Windows

First, install Ubuntu on Windows 10 [following this tutorial](https://tutorials.ubuntu.com/tutorial/tutorial-ubuntu-on-windows#0).

Then install the required packages by running the following commands in the Ubuntu terminal:

```bash
sudo apt update
sudo apt install python3 python3-virtualenv
```

While using Ubuntu on Windows, **follow the instructions for Linux**.

### Option 2: Install Python on Windows

#### Download and install Python

1. [Download Python](https://www.python.org/downloads/windows/) (version 3.5.4 is recommended)
2. Run the downloaded installer
3.  Click 'Customize installation'
4. Check if 'pip' is selected for installation
5. Select 'Add Python to environment variables'
6. Finish the installation

#### Check if python is in your PATH
  After the installation is complete double check to make sure you see python in your PATH.
  You can find your path by opening your *Control panel
  -> System and Security -> System -> Advanced System Settings -> Environment Variables -> Selecting Path -> Edit*.

#### Install the environment
Run command line (cmd) and install virtualenv.

```bash
pip install virtualenv
```

The output is expected to include:


```bash
Installing collected packages: virtualenv
Successfully installed virtualenv-16.0.0
```

Install virtualenvwrapper-win:

```bash
pip install virtualenvwrapper-win
```

The output is expected to include:

```bash
Installing collected packages: virtualenvwrapper-win
  Running setup.py install for virtualenvwrapper-win ... done
Successfully installed virtualenvwrapper-win-1.2.5
```